"""
prompt_chain_executor.py
========================
Core execution engine for Prompt Chains.

How chaining works
------------------
Each chain is an ordered list of steps.  A shared *context* dict is passed
through every step:

    context = {"input": <initial_input>}

Before calling the LLM for step N, its ``prompt_template`` is rendered by
substituting every ``{{key}}`` placeholder with the matching value from the
context.  After the LLM responds, the raw text is stored in the context under
the step's ``output_variable``.  Step N+1 therefore has access to every
output produced by all preceding steps.

Template variable resolution order (highest wins):
  1. context (accumulated previous outputs)
  2. step["variables"]  (static values defined at authoring time)

Public API
----------
    result = execute_chain(chain_id, initial_input)

    result = {
        "run_id":       int,
        "chain_id":     int,
        "status":       "completed" | "failed",
        "final_output": str,          # last step's output
        "context":      dict,         # full accumulated context
        "steps": [
            {
                "step_order":    int,
                "name":          str,
                "input_sent":    str,   # rendered prompt
                "output":        str,   # LLM response
                "status":        "completed" | "failed",
                "duration_ms":   int,
            },
            ...
        ],
        "error": str | None,
    }

Streaming callback
------------------
Pass ``on_step_complete`` to receive live updates as each step finishes:

    def my_callback(step_order, output, context):
        print(f"Step {step_order} done: {output[:80]}")

    execute_chain(chain_id, text, on_step_complete=my_callback)
"""

import re
import time
import datetime
import requests
from typing import Callable, Any

import config
from llm_providers import LLMClient
from prompt_chain_db import (
    get_chain,
    create_run, update_run_step, finish_run,
)
from database import get_cached_comments, get_summary, get_custom_fields, init_db, search_cached_issues_by_product_keyword


# ---------------------------------------------------------------------------
# Date formatting
# ---------------------------------------------------------------------------

def _format_date(date_val):
    """Convert .NET JSON date format to human readable."""
    if not date_val:
        return "Unknown"
    if isinstance(date_val, str) and date_val.startswith("/Date("):
        match = re.search(r"/Date\((\d+)", date_val)
        if match:
            try:
                ts = int(match.group(1)) / 1000
                return datetime.datetime.fromtimestamp(ts).strftime("%Y-%m-%d %H:%M")
            except (ValueError, OSError):
                pass
    return str(date_val)


def _extract_keywords_from_search_terms(search_terms_output: str) -> list[str]:
    """Extract search keywords from LLM-generated search terms output."""
    if not search_terms_output:
        return []

    keywords = []

    primary_match = re.search(r"PRIMARY SEARCH TERM:\s*(.+?)(?:\n|$)", search_terms_output, re.IGNORECASE)
    if primary_match:
        primary = primary_match.group(1).strip()
        if primary and primary.lower() != "none":
            keywords.append(primary)

    secondary_match = re.search(r"SECONDARY SEARCH TERMS:(.+?)(?:COMPONENT|$)", search_terms_output, re.IGNORECASE | re.DOTALL)
    if secondary_match:
        secondary_text = secondary_match.group(1)
        for line in secondary_text.split("\n"):
            line = line.strip().lstrip("-•* ")
            if line and line.lower() != "none" and len(line) > 1:
                keywords.append(line)

    if not keywords:
        words = re.findall(r'\b[A-Z][a-z]+\b', search_terms_output)
        keywords = [w for w in words if w.lower() not in ('none', 'primary', 'secondary', 'search', 'term', 'component', 'filter', 'strict', 'matching', 'rule')]

    return keywords


def _auto_search_similar_issues(context: dict, search_terms_output: str) -> str:
    """
    Extract keywords from search_terms output and search the database.
    
    Returns formatted similar issues string for injection into context.
    """
    keywords = _extract_keywords_from_search_terms(search_terms_output)
    if not keywords:
        return "No search terms generated."
    
    # Get product from context
    product = context.get("cached_product", "")
    if product and product.lower() == "not recorded":
        product = None
    
    # Search database
    try:
        results = search_cached_issues_by_product_keyword(product, keywords, limit=10)
        
        # Fallback: if no results with product filter, search all products
        if not results and product:
            results = search_cached_issues_by_product_keyword(None, keywords, limit=10)
    except Exception as e:
        return f"Error searching database: {e}"
    
    if not results:
        if product:
            return f"No similar issues found for product '{product}' with keywords: {', '.join(keywords)}"
        else:
            return f"No similar issues found with keywords: {', '.join(keywords)}"
    
    # Format results
    formatted = []
    for i, result in enumerate(results, 1):
        formatted.append(f"SIMILAR ISSUE #{i} (Request ID: {result['request_id']})")
        formatted.append(f"Match Reason: {result['match_reason']}")
        formatted.append(f"Product: {result.get('product', 'Unknown')}")
        formatted.append("---")
        formatted.append(result["text"])
        formatted.append("\n")
    
    return "\n".join(formatted)


# ---------------------------------------------------------------------------
# Template rendering
# ---------------------------------------------------------------------------

_PLACEHOLDER_RE = re.compile(r"\{\{(\w+)\}\}")


def render_template(template: str, context: dict) -> str:
    """Substitute ``{{key}}`` placeholders from *context*.

    Unknown keys are left as-is so the prompt author can spot them easily.
    """
    def _replace(match: re.Match) -> str:
        key = match.group(1)
        return str(context.get(key, match.group(0)))   # keep original if missing

    return _PLACEHOLDER_RE.sub(_replace, template)


# ---------------------------------------------------------------------------
# LLM call (same pattern as analysis.py)
# ---------------------------------------------------------------------------

def _call_llm(prompt: str, model: str | None = None,
               temperature: float = 0.3, timeout: int = 300) -> str:
    """Send ``prompt`` to LLM and return the response text.

    Raises ``RuntimeError`` on network / API failure so the executor can
    catch it and mark the step as failed.
    """
    llm = config.initialize_llm()
    try:
        content = llm.generate(prompt, temperature=temperature)
        if not content:
            raise RuntimeError("LLM returned an empty response")
        return content
    except Exception as exc:
        raise RuntimeError(f"LLM request failed: {exc}") from exc


# ---------------------------------------------------------------------------
# Database query handler
# ---------------------------------------------------------------------------

def _handle_db_query(step: dict, context: dict) -> str:
    """Handle db_query step type - fetch data from database and return as text.

    The step's prompt_template defines the query type:
    - "get_comments"     : fetch comments for the request ID in {{input}}
    - "get_summary"      : fetch existing summary for request ID in {{input}}
    - "get_custom_fields": fetch custom fields for request ID in {{input}}
    - "get_all"          : fetch all cached data (comments, summary, custom fields)
    - "search_keywords"  : search cached issues by keywords from {{search_terms}}

    The result is stored in context under output_variable.
    """
    query_type = step.get("prompt_template", "").strip().lower()

    # ── search_keywords: search by keywords from context ──────────────
    if query_type == "search_keywords":
        search_terms = context.get("search_terms", "")
        if not search_terms:
            return "No search terms available in context."

        keywords = _extract_keywords_from_search_terms(search_terms)
        if not keywords:
            return "No keywords could be extracted from search terms."

        product = context.get("cached_product", "")
        if product and product.lower() == "not recorded":
            product = None

        try:
            results = search_cached_issues_by_product_keyword(product, keywords, limit=10)
            if not results and product:
                results = search_cached_issues_by_product_keyword(None, keywords, limit=10)
        except Exception as e:
            return f"Error searching database: {e}"

        if not results:
            if product:
                return f"No similar issues found for product '{product}' with keywords: {', '.join(keywords)}"
            return f"No similar issues found with keywords: {', '.join(keywords)}"

        formatted = []
        for i, result in enumerate(results, 1):
            formatted.append(f"SIMILAR ISSUE #{i} (Request ID: {result['request_id']})")
            formatted.append(f"Match Reason: {result['match_reason']}")
            formatted.append(f"Product: {result.get('product', 'Unknown')}")
            formatted.append("---")
            formatted.append(result["text"])
            formatted.append("\n")

        return "\n".join(formatted)

    request_id = context.get("input", "").strip()

    if not request_id:
        raise RuntimeError("No request ID provided in input")

    try:
        request_id = int(request_id)
    except ValueError:
        raise RuntimeError(f"Invalid request ID: {request_id}")

    result_parts = []

    if query_type in ("get_comments", "get_all"):
        comments, _ = get_cached_comments(request_id)
        if comments:
            result_parts.append("COMMENTS:\n---")
            for i, c in enumerate(comments[:50]):
                date = _format_date(c.get("date"))
                text = c.get("text", "")
                result_parts.append(f"[{date}] COMMENT {i+1}:\n{text}\n---")
        else:
            result_parts.append("COMMENTS: None found")

    if query_type in ("get_summary", "get_all"):
        summary, created_at = get_summary(request_id)
        if summary:
            result_parts.append(f"\nEXISTING SUMMARY (created {created_at}):\n{summary}")
        else:
            result_parts.append("\nEXISTING SUMMARY: None available")

    if query_type in ("get_custom_fields", "get_all"):
        fields, _ = get_custom_fields(request_id)
        if fields:
            result_parts.append("\nCUSTOM FIELDS:")
            for fname, fvalue in fields.items():
                result_parts.append(f"  {fname}: {fvalue}")
        else:
            result_parts.append("\nCUSTOM FIELDS: None recorded")

    if not result_parts:
        raise RuntimeError(f"Unknown query type: {query_type}")

    return "\n".join(result_parts)


# ---------------------------------------------------------------------------
# Core execution function
# ---------------------------------------------------------------------------

def execute_chain(
    chain_id: int,
    initial_input: str,
    model: str | None = None,
    temperature: float = 0.3,
    on_step_complete: Callable[[int, str, dict], None] | None = None,
    progress_callback: Callable[[str], None] | None = None,
) -> dict:
    """Execute a saved prompt chain end-to-end and return a structured result.

    Parameters
    ----------
    chain_id:
        ID of the chain to run (must exist in the database).
    initial_input:
        The seed text placed into ``context["input"]`` before step 1 runs.
    model:
        Ollama model name override; defaults to ``OLLAMA_MODEL`` from config.
    temperature:
        LLM sampling temperature (0 = deterministic, 1 = creative).
    on_step_complete:
        Optional callback called after each step with
        ``(step_order, output_text, current_context)``.
    progress_callback:
        Optional callback for status strings, e.g. to drive a UI label.

    Returns
    -------
    dict
        See module docstring for the full shape.
    """

    def _progress(msg: str) -> None:
        if progress_callback:
            progress_callback(msg)

    # ── 1. Load chain ──────────────────────────────────────────────
    chain = get_chain(chain_id)
    if not chain:
        return {
            "run_id": None, "chain_id": chain_id,
            "status": "failed", "final_output": None,
            "context": {}, "steps": [],
            "error": f"Chain {chain_id} not found",
        }

    steps = chain.get("steps", [])
    if not steps:
        return {
            "run_id": None, "chain_id": chain_id,
            "status": "failed", "final_output": None,
            "context": {"input": initial_input}, "steps": [],
            "error": "Chain has no steps",
        }

    # ── 2. Create run record ───────────────────────────────────────
    run_id = create_run(chain_id, initial_input)

    # ── 3. Build initial context ───────────────────────────────────
    # "input" is always available; steps may add further keys.
    context: dict[str, Any] = {"input": initial_input}

    # Auto-fetch cached data if input is numeric (request ID)
    cached_data_warning = None
    try:
        request_id = int(initial_input.strip())
        comments, _ = get_cached_comments(request_id)
        fields, _ = get_custom_fields(request_id)

        if not comments and not fields:
            cached_data_warning = f"No cached data found for request {request_id}. Please download it first using the Search Cache."

        # Inject cached data into context
        if comments:
            context["cached_comments"] = "\n\n".join([
                f"[{_format_date(c.get('date'))}] {c.get('text', '')}"
                for c in comments[:50]
            ])
        else:
            context["cached_comments"] = "No cached comments found"

        # Fetch summary
        summary, _ = get_summary(request_id)
        if summary:
            context["cached_summary"] = summary
        else:
            context["cached_summary"] = "No existing summary"

        if fields:
            context["cached_client"] = fields.get("Client", "Not recorded")
            context["cached_product"] = fields.get("Product", "Not recorded")
            context["cached_release_version"] = fields.get("Release Version", "Not recorded")
            context["cached_site"] = fields.get("Site", "Not recorded")
        else:
            context["cached_client"] = "Not recorded"
            context["cached_product"] = "Not recorded"
            context["cached_release_version"] = "Not recorded"
            context["cached_site"] = "Not recorded"

    except ValueError:
        # Not a numeric request ID - skip cached data fetch
        context["cached_comments"] = "No input (not a request ID)"
        context["cached_summary"] = "No input (not a request ID)"
        context["cached_client"] = "No input (not a request ID)"
        context["cached_product"] = "No input (not a request ID)"
        context["cached_release_version"] = "No input (not a request ID)"
        context["cached_site"] = "No input (not a request ID)"

    if cached_data_warning:
        _progress(cached_data_warning)

    step_results: list[dict] = []
    last_output: str = initial_input
    chain_error: str | None = None

    # ── 4. Execute each step in order ─────────────────────────────
    for step in steps:
        order   = step["step_order"]
        name    = step.get("name") or f"Step {order}"
        out_var = step["output_variable"]

        _progress(f"Running step {order}/{len(steps)}: {name}…")

        step_result: dict = {
            "step_order":  order,
            "name":        name,
            "input_sent":  None,
            "output":      None,
            "status":      "pending",
            "duration_ms": None,
            "error":       None,
        }

        t_start = time.monotonic()

        # Handle different step types
        step_type = step.get("step_type", "llm")
        if step_type == "db_query":
            try:
                output_text = _handle_db_query(step, context)
                duration_ms = int((time.monotonic() - t_start) * 1000)
                step_result["input_sent"] = f"DB Query: {step.get('prompt_template', '')}"
            except Exception as e:
                duration_ms = int((time.monotonic() - t_start) * 1000)
                error_msg = str(e)
                step_result.update({
                    "status":      "failed",
                    "error":       error_msg,
                    "duration_ms": duration_ms,
                })
                chain_error = f"Step {order} ({name}) failed: {error_msg}"
                update_run_step(
                    run_id=run_id,
                    step_id=step["id"],
                    step_order=order,
                    input_sent=step_result["input_sent"],
                    output_received=None,
                    status="failed",
                    error=error_msg,
                    duration_ms=duration_ms,
                )
                step_results.append(step_result)
                _progress(f"Chain failed at step {order}: {error_msg}")
                break
        else:
            # Merge static step variables into context (context values win)
            merged_context = {**step.get("variables", {}), **context}

            # Render the prompt
            rendered_prompt = render_template(step["prompt_template"], merged_context)
            step_result["input_sent"] = rendered_prompt

            try:
                output_text = _call_llm(
                    rendered_prompt,
                    model=model,
                    temperature=temperature,
                )
                duration_ms = int((time.monotonic() - t_start) * 1000)

                # Store output in context under the declared key
                context[out_var] = output_text
                last_output = output_text

                step_result.update({
                    "output":      output_text,
                    "status":      "completed",
                    "duration_ms": duration_ms,
                })

                # Persist step result
                update_run_step(
                    run_id=run_id,
                    step_id=step["id"],
                    step_order=order,
                    input_sent=rendered_prompt,
                    output_received=output_text,
                    status="completed",
                    duration_ms=duration_ms,
                )

                # Auto-search for similar issues if this step produces search_terms
                if out_var == "search_terms":
                    similar_issues = _auto_search_similar_issues(context, output_text)
                    context["similar_issues"] = similar_issues

                if on_step_complete:
                    on_step_complete(order, output_text, dict(context))

            except RuntimeError as exc:
                duration_ms = int((time.monotonic() - t_start) * 1000)
                error_msg = str(exc)
                step_result.update({
                    "status":      "failed",
                    "error":       error_msg,
                    "duration_ms": duration_ms,
                })
                chain_error = f"Step {order} ({name}) failed: {error_msg}"

                update_run_step(
                    run_id=run_id,
                    step_id=step["id"],
                    step_order=order,
                    input_sent=rendered_prompt,
                    output_received=None,
                    status="failed",
                    error=error_msg,
                    duration_ms=duration_ms,
                )

                step_results.append(step_result)
                _progress(f"Chain failed at step {order}: {error_msg}")
                break   # abort on first failure

        step_results.append(step_result)

    # ── 5. Finalise run ────────────────────────────────────────────
    final_status = "failed" if chain_error else "completed"
    final_output = None if chain_error else last_output

    finish_run(
        run_id=run_id,
        status=final_status,
        final_output=final_output,
        error=chain_error,
    )

    _progress("Chain complete." if not chain_error else f"Chain failed: {chain_error}")

    return {
        "run_id":       run_id,
        "chain_id":     chain_id,
        "status":       final_status,
        "final_output": final_output,
        "context":      context,
        "steps":        step_results,
        "error":        chain_error,
    }


# ---------------------------------------------------------------------------
# Convenience: execute by chain name
# ---------------------------------------------------------------------------

def execute_chain_by_name(
    chain_name: str,
    initial_input: str,
    **kwargs,
) -> dict:
    """Look up a chain by name and execute it.

    Raises ``ValueError`` if no chain with that name exists.
    """
    from prompt_chain_db import list_chains
    chains = list_chains()
    match = next((c for c in chains if c["name"] == chain_name), None)
    if not match:
        raise ValueError(f"No chain named '{chain_name}'")
    return execute_chain(match["id"], initial_input, **kwargs)


# ---------------------------------------------------------------------------
# Quick smoke-test  (python prompt_chain_executor.py)
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    from prompt_chain_db import init_chain_db, save_chain, delete_chain

    init_chain_db()

    # Build a two-step demo chain
    cid = save_chain(
        name="Demo: Classify then Summarise",
        description="Two-step demo — classification feeds into summary.",
        steps=[
            {
                "step_order": 1,
                "name": "Classify",
                "prompt_template": (
                    "Classify the following support ticket into exactly one "
                    "category: Bug, Feature Request, Question, or Complaint. "
                    "Reply with just the category name.\n\nTicket:\n{{input}}"
                ),
                "input_variable": "input",
                "output_variable": "classification",
            },
            {
                "step_order": 2,
                "name": "Summarise",
                "prompt_template": (
                    "You are a support summariser.\n"
                    "Category: {{classification}}\n\n"
                    "Write a one-sentence customer-facing summary.\n\n"
                    "Original ticket:\n{{input}}"
                ),
                "input_variable": "classification",
                "output_variable": "summary",
            },
        ],
    )

    sample = "My dashboard stopped loading after the 4.2 upgrade. I get a blank screen."

    print(f"\nExecuting chain ID {cid} …\n{'='*60}")
    result = execute_chain(
        cid,
        sample,
        progress_callback=lambda m: print(f"  [{m}]"),
        on_step_complete=lambda order, out, ctx: print(f"\n  Step {order} output:\n  {out}\n"),
    )

    print(f"\nFinal status : {result['status']}")
    print(f"Final output : {result['final_output']}")
    print(f"Full context : {result['context']}")

    delete_chain(cid)
    print("\nDemo chain cleaned up.")
