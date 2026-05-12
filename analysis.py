import json
import requests
import re

import config
from llm_providers import LLMClient
from database import get_prompt as db_get_prompt, DEFAULT_PROMPTS


def get_llm():
    """Get initialized LLM client."""
    return config.initialize_llm()


def clean_html(text):
    """
    Convert HTML comment text to clean plain text, preserving readable structure.

    - Block-level elements (p, div, br, li, headings) become newlines
    - Inline elements (span, strong, em, etc.) are unwrapped transparently
    - HTML entities are decoded
    - Consecutive blank lines are collapsed to a single blank line
    """
    if not text:
        return ""

    # Remove non-content elements entirely
    text = re.sub(r'<style[^>]*>.*?</style>', '', text, flags=re.DOTALL | re.IGNORECASE)
    text = re.sub(r'<script[^>]*>.*?</script>', '', text, flags=re.DOTALL | re.IGNORECASE)

    # Block/structural elements → newlines (before stripping tags)
    # Double newline for paragraph-level breaks
    text = re.sub(r'</p\s*>', '\n\n', text, flags=re.IGNORECASE)
    text = re.sub(r'</div\s*>', '\n', text, flags=re.IGNORECASE)
    text = re.sub(r'<br\s*/?>', '\n', text, flags=re.IGNORECASE)
    text = re.sub(r'<hr\s*/?>', '\n', text, flags=re.IGNORECASE)
    text = re.sub(r'</tr\s*>', '\n', text, flags=re.IGNORECASE)
    text = re.sub(r'</td\s*>', '  ', text, flags=re.IGNORECASE)
    text = re.sub(r'</th\s*>', '  ', text, flags=re.IGNORECASE)

    # List items get a bullet marker
    text = re.sub(r'<li[^>]*>', '\n• ', text, flags=re.IGNORECASE)
    text = re.sub(r'</li\s*>', '', text, flags=re.IGNORECASE)

    # Headings get a double newline after
    text = re.sub(r'</h[1-6]\s*>', '\n\n', text, flags=re.IGNORECASE)

    # Strip all remaining tags
    text = re.sub(r'<[^>]+>', '', text)

    # Decode HTML entities
    text = re.sub(r'&#(\d+);', lambda m: chr(int(m.group(1))), text)
    text = re.sub(r'&#x([0-9a-fA-F]+);', lambda m: chr(int(m.group(1), 16)), text)
    text = text.replace('&nbsp;', ' ')
    text = text.replace('&amp;', '&')
    text = text.replace('&lt;', '<')
    text = text.replace('&gt;', '>')
    text = text.replace('&quot;', '"')
    text = text.replace('&apos;', "'")
    text = text.replace('&hellip;', '...')
    text = text.replace('&mdash;', '—')
    text = text.replace('&ndash;', '–')
    text = text.replace('&lsquo;', '\u2018')
    text = text.replace('&rsquo;', '\u2019')
    text = text.replace('&ldquo;', '\u201c')
    text = text.replace('&rdquo;', '\u201d')

    # Normalise whitespace: trim each line, collapse blanks
    lines = [line.rstrip() for line in text.splitlines()]
    # Collapse runs of more than one blank line into a single blank line
    result = []
    prev_blank = False
    for line in lines:
        is_blank = line.strip() == ''
        if is_blank and prev_blank:
            continue
        result.append(line)
        prev_blank = is_blank

    return '\n'.join(result).strip()


def clean_html_tags(text):
    """Alias for clean_html — kept for backward compatibility."""
    return clean_html(text)


def deduplicate_comment_dicts(comments_list):
    """
    Deduplicate lines across a list of comment dicts.
    Comments are already stored as clean plain text — no HTML cleaning needed here.
    """
    seen = set()
    unique_lines = []
    for comment in comments_list:
        text = comment.get("text", "")
        for line in text.split('\n'):
            line = line.strip()
            if line and line not in seen and len(line) > 5:
                seen.add(line)
                unique_lines.append(line)
    return unique_lines


def deduplicate_text(text):
    seen = set()
    result = []
    for line in text.split('\n'):
        line = line.strip()
        if line and line not in seen and len(line) > 3:
            seen.add(line)
            result.append(line)
    return '\n'.join(result)


def extract_issues_batch(comments_list):
    """
    Analyze support comments to extract issue categories/types.
    Uses LLM to classify each ticket into categories like:
    - Bug Report
    - Feature Request
    - Performance Issue
    - Data/Integration Issue
    - User Training/Question
    - Account/Access Issue
    - Configuration Issue
    - Other
    """
    if not comments_list:
        return []

    base_prompt = get_prompt("extract_issues")
    all_issues = []
    llm = get_llm()

    for comment_text in comments_list:
        prompt = base_prompt.format(comments=comment_text[:3000])

        for retry in range(3):
            try:
                content = llm.generate(prompt, temperature=0.2)
                if content and content.lower() != "unclassified":
                    categories = [c.strip() for c in content.split(",")]
                    all_issues.extend(categories)
                break
            except Exception as e:
                if retry == 2:
                    print(f"Error extracting issues: {e}")
                else:
                    print(f"Retry {retry + 1}/3...")

    return all_issues


def get_prompt(name):
    """Get prompt from database, fallback to default if not found."""
    try:
        result = db_get_prompt(name)
        if result:
            return result["content"]
    except Exception:
        pass
    return DEFAULT_PROMPTS.get(name, "")


def summarize_comments(comments_text, request_id=None):
    prompt = f"""Summarize this support request conversation in 2-3 sentences.

Include:
- What the issue was
- What action was taken or requested
- Current status/resolution

Keep it concise and professional.

CONVERSATION:
{comments_text}
"""
    llm = get_llm()
    try:
        return llm.generate(prompt, temperature=0.3)
    except Exception as e:
        return f"Error: {e}"


def summarize_batch(texts, batch_size=1):
    results = []
    base_prompt = get_prompt("summarize")
    llm = get_llm()

    for i in range(0, len(texts), batch_size):
        batch = texts[i:i + batch_size]

        prompt = base_prompt
        for item in batch:
            prompt += "\n" + item + "\n"

        for retry in range(3):
            try:
                content = llm.generate(prompt, temperature=0.3)
                results.append(content)
                break
            except Exception as e:
                if retry == 2:
                    results.append(f"Error processing: {e}")
                else:
                    print(f"Retry {retry + 1}/3...")

        if i + batch_size < len(texts):
            print(f"Processed {i + batch_size}/{len(texts)} requests...")

    return results


def refine_search_query(query):
    base_prompt = get_prompt("refine_search")
    prompt = base_prompt.format(query=query)
    try:
        llm = get_llm()
        content = llm.generate(prompt, temperature=0.3)
        terms = [term.strip() for term in content.split('\n') if term.strip()]
        return [query] + terms
    except Exception as e:
        return [query]


def summarize_search_results(matches, query, custom_prompt=""):
    if not matches:
        return "No matching results found."
    
    results_text = "\n\n".join([
        f"[Request #{m['request_id']} ({m['source']})]\n{m['text'][:500]}"
        for m in matches[:20]
    ])
    
    base_prompt = get_prompt("summarize_search")
    default_prompt = base_prompt.format(
        query=query,
        match_count=len(matches),
        results_text=results_text
    )

    if custom_prompt:
        prompt = default_prompt + "\n\n" + custom_prompt
    else:
        prompt = default_prompt

    llm = get_llm()
    try:
        return llm.generate(prompt, temperature=0.3)
    except Exception as e:
        return f"Error generating summary: {e}"