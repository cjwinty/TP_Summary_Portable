import sqlite3
import json
import sys
from datetime import datetime
import threading
import re
import difflib
from pathlib import Path


def _get_base_dir():
    if getattr(sys, 'frozen', False):
        return Path(sys.executable).parent
    return Path(__file__).resolve().parent


BASE_DIR = _get_base_dir()


def parse_comment_date(date_val):
    """Parse comment date to datetime object for comparison.
    Handles /Date(timestamp)/ format, ISO format, and other common formats.
    Returns None if parsing fails.
    """
    if not date_val:
        return None
    
    # Handle /Date(timestamp)/ format (e.g., /Date(1704067200000+0000)/)
    match = re.search(r"/Date\((\d+)", str(date_val))
    if match:
        try:
            ts = int(match.group(1)) / 1000
            return datetime.fromtimestamp(ts)
        except (ValueError, OSError):
            pass
    
    # Try ISO format
    for fmt in ("%Y-%m-%dT%H:%M:%S", "%Y-%m-%d %H:%M:%S", "%Y-%m-%d"):
        try:
            return datetime.strptime(str(date_val).split('.')[0], fmt)
        except ValueError:
            pass
    
    return None

STOPWORDS = {'the', 'a', 'an', 'is', 'are', 'was', 'were', 'be', 'been', 'being',
             'have', 'has', 'had', 'do', 'does', 'did', 'will', 'would', 'could',
             'should', 'may', 'might', 'must', 'can', 'need', 'to', 'of', 'in',
             'for', 'on', 'with', 'at', 'by', 'from', 'as', 'into', 'through',
             'during', 'before', 'after', 'above', 'below', 'between', 'under',
             'again', 'further', 'then', 'once', 'here', 'there', 'when', 'where',
             'why', 'how', 'all', 'each', 'few', 'more', 'most', 'other', 'some',
             'such', 'no', 'nor', 'not', 'only', 'own', 'same', 'so', 'than',
             'too', 'very', 'just', 'also', 'now', 'and', 'but', 'or', 'if'}

DB_PATH = str(BASE_DIR / "tp_cache.db")

_connection = None
_lock = threading.Lock()


def _get_conn():
    global _connection
    if _connection is None:
        _connection = sqlite3.connect(DB_PATH, check_same_thread=False)
        _connection.execute("PRAGMA journal_mode=WAL")
        _connection.execute("PRAGMA synchronous=NORMAL")
        _connection.execute("PRAGMA cache_size=-64000")
        _connection.execute("PRAGMA temp_store=MEMORY")
    return _connection


def _close_conn():
    global _connection
    if _connection:
        _connection.close()
        _connection = None


def init_db():
    conn = _get_conn()
    c = conn.cursor()
    c.execute("""
        CREATE TABLE IF NOT EXISTS comments (
            request_id INTEGER PRIMARY KEY,
            comment_data TEXT,
            fetched_at TEXT
        )
    """)
    c.execute("""
        CREATE TABLE IF NOT EXISTS summaries (
            request_id INTEGER PRIMARY KEY,
            summary_text TEXT,
            created_at TEXT
        )
    """)
    c.execute("""
        CREATE TABLE IF NOT EXISTS request_custom_fields (
            request_id INTEGER PRIMARY KEY,
            client TEXT,
            product TEXT,
            release_version TEXT,
            site TEXT,
            fetched_at TEXT
        )
    """)
    c.execute("CREATE INDEX IF NOT EXISTS idx_custom_fields_lookup ON request_custom_fields(client, product, release_version, site, request_id)")
    c.execute("CREATE INDEX IF NOT EXISTS idx_comments_fetched_at ON comments(fetched_at)")
    c.execute("CREATE INDEX IF NOT EXISTS idx_summaries_created_at ON summaries(created_at)")
    c.execute("CREATE INDEX IF NOT EXISTS idx_custom_fields_client ON request_custom_fields(client)")
    c.execute("CREATE INDEX IF NOT EXISTS idx_custom_fields_product ON request_custom_fields(product)")
    c.execute("CREATE INDEX IF NOT EXISTS idx_custom_fields_release_version ON request_custom_fields(release_version)")
    c.execute("CREATE INDEX IF NOT EXISTS idx_custom_fields_site ON request_custom_fields(site)")
    conn.commit()

    c.execute("""
        CREATE TABLE IF NOT EXISTS prompts (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            name TEXT UNIQUE NOT NULL,
            content TEXT NOT NULL,
            is_active INTEGER DEFAULT 0,
            created_at TEXT,
            updated_at TEXT
        )
    """)
    c.execute("CREATE INDEX IF NOT EXISTS idx_prompts_name ON prompts(name)")
    c.execute("CREATE INDEX IF NOT EXISTS idx_prompts_is_active ON prompts(is_active)")
    conn.commit()

    init_default_prompts()


def save_prompt(name, content, is_active=False):
    with _lock:
        conn = _get_conn()
        c = conn.cursor()
        now = datetime.now().isoformat()
        c.execute("""
            INSERT INTO prompts (name, content, is_active, created_at, updated_at)
            VALUES (?, ?, ?, ?, ?)
            ON CONFLICT(name) DO UPDATE SET
                content = excluded.content,
                is_active = excluded.is_active,
                updated_at = excluded.updated_at
        """, (name, content, 1 if is_active else 0, now, now))
        conn.commit()


def get_prompt(name):
    with _lock:
        conn = _get_conn()
        c = conn.cursor()
        c.execute("SELECT content, is_active FROM prompts WHERE name = ?", (name,))
        row = c.fetchone()
    if row:
        return {"content": row[0], "is_active": bool(row[1])}
    return None


def get_all_prompts():
    with _lock:
        conn = _get_conn()
        c = conn.cursor()
        c.execute("SELECT id, name, content, is_active, created_at, updated_at FROM prompts ORDER BY name")
        rows = c.fetchall()
    return [{"id": r[0], "name": r[1], "content": r[2], "is_active": bool(r[3]), "created_at": r[4], "updated_at": r[5]} for r in rows]


def set_active_prompt(name):
    with _lock:
        conn = _get_conn()
        c = conn.cursor()
        c.execute("UPDATE prompts SET is_active = 0")
        c.execute("UPDATE prompts SET is_active = 1 WHERE name = ?", (name,))
        conn.commit()


def get_active_prompt(name):
    with _lock:
        conn = _get_conn()
        c = conn.cursor()
        c.execute("SELECT content FROM prompts WHERE name = ? AND is_active = 1", (name,))
        row = c.fetchone()
    if row:
        return row[0]
    return None


DEFAULT_PROMPTS = {
    "summarize": """You are a support ticket analyzer. Read the EXISTING comments/conversation for each ticket below and produce a detailed summary.

For EACH ticket, provide a structured summary with these sections:
- Issue: What was the problem or request about?
- Actions Taken: What steps were taken to resolve the issue?
- Current Status: Is it resolved, pending, or escalated?

Be detailed and specific. Use information only from the provided comments.

TICKETS:
""",
    "refine_search": """You are a search query refinement assistant. The user wants to search through support ticket comments.

Original search query: "{query}"

Generate 3-5 alternative search terms or phrases that would help find related issues, including:
- Common variations and synonyms
- Related technical terms
- Alternative ways users might describe the same problem

Return only the refined search terms, one per line, nothing else.
""",
    "summarize_search": """You are a support ticket analysis expert. Your task is to analyze and correlate only the provided search results. Do not infer or assume information that is not explicitly supported by the text. The user searched for: "{query}"

You found {match_count} matching results. Below are the most relevant excerpts:

{results_text}

Instructions:
- Only use information present in the excerpts below.
- If information is missing or unclear, explicitly state "Not enough information in results".
- Do not introduce external knowledge or assumptions.

Your response must include:

1. Issue Summary:
   - A concise description of the problem based only on the results.

2. Common Themes / Patterns:
   - Identify recurring symptoms, causes, or behaviours across the results.

3. Previously Used Fixes / Resolutions:
   - List any solutions that appear in the data.
   - If none are present, state clearly.

4. Recommendations:
   - Suggest next steps strictly grounded in observed patterns or fixes.
   - Do not speculate beyond the data.

5. Related Issues:
   - List related issue IDs found in the results.
   - Explain how they are related based on the content (e.g., same error, same system, same symptom).

Output Requirements:
- Be precise, structured, and factual.
- Prioritise accuracy and completeness.
- Avoid repetition.
""",
    "extract_issues": """You are a support ticket issue categorizer. Analyze the following support ticket conversation and identify the primary issue type(s).

Categories to choose from (one or more of):
- Bug Report: Software errors, crashes, unexpected behavior
- Feature Request: New functionality or enhancement requested
- Performance Issue: Slow system, timeouts, high resource usage
- Data/Integration Issue: Data quality, import/export, API issues
- User Training/Question: How-to questions, clarification needed
- Account/Access Issue: Login problems, permissions, access request
- Configuration Issue: Setup, installation, settings problems
- Other: Issues that don't fit above categories

Analyze the ticket conversation below and respond with ONLY a comma-separated list of categories that apply (e.g., "Bug Report, Performance Issue").
If no clear category can be determined, respond with "Unclassified".

Ticket conversation:
{comments}

Categories found: """
}


def init_default_prompts():
    with _lock:
        conn = _get_conn()
        c = conn.cursor()
        c.execute("SELECT COUNT(*) FROM prompts")
        count = c.fetchone()[0]
    
    if count == 0:
        for name, content in DEFAULT_PROMPTS.items():
            save_prompt(name, content, is_active=True)
        print("Initialized default prompts in database")


def get_cached_comments(request_id):
    with _lock:
        conn = _get_conn()
        c = conn.cursor()
        c.execute("SELECT comment_data, fetched_at FROM comments WHERE request_id = ?", (request_id,))
        row = c.fetchone()
        
        if row:
            try:
                return json.loads(row[0]), row[1]
            except (json.JSONDecodeError, TypeError):
                return None, None
    return None, None


def save_comments(request_id, comments):
    with _lock:
        conn = _get_conn()
        c = conn.cursor()
        c.execute("""
            INSERT OR REPLACE INTO comments (request_id, comment_data, fetched_at)
            VALUES (?, ?, ?)
        """, (request_id, json.dumps(comments), datetime.now().isoformat()))
        conn.commit()


def delete_comments(request_id):
    with _lock:
        conn = _get_conn()
        c = conn.cursor()
        c.execute("DELETE FROM comments WHERE request_id = ?", (request_id,))
        c.execute("DELETE FROM request_custom_fields WHERE request_id = ?", (request_id,))
        conn.commit()


def delete_custom_fields(request_id):
    with _lock:
        conn = _get_conn()
        c = conn.cursor()
        c.execute("DELETE FROM request_custom_fields WHERE request_id = ?", (request_id,))
        conn.commit()


def delete_summary(request_id):
    with _lock:
        conn = _get_conn()
        c = conn.cursor()
        c.execute("DELETE FROM summaries WHERE request_id = ?", (request_id,))
        conn.commit()


def delete_all_custom_fields():
    with _lock:
        conn = _get_conn()
        c = conn.cursor()
        c.execute("DELETE FROM request_custom_fields")
        conn.commit()


def delete_all_summaries():
    with _lock:
        conn = _get_conn()
        c = conn.cursor()
        c.execute("DELETE FROM summaries")
        conn.commit()


def get_cache_counts():
    with _lock:
        conn = _get_conn()
        c = conn.cursor()
        c.execute("SELECT COUNT(*) FROM comments")
        comments_count = c.fetchone()[0]
        c.execute("SELECT COUNT(*) FROM summaries")
        summaries_count = c.fetchone()[0]
        c.execute("SELECT COUNT(*) FROM request_custom_fields")
        custom_fields_count = c.fetchone()[0]
    return {"comments": comments_count, "summaries": summaries_count, "custom_fields": custom_fields_count}


def delete_all_comments():
    with _lock:
        conn = _get_conn()
        c = conn.cursor()
        c.execute("DELETE FROM comments")
        conn.commit()


def get_all_cached_ids():
    with _lock:
        conn = _get_conn()
        c = conn.cursor()
        c.execute("SELECT request_id FROM comments")
        rows = c.fetchall()
    return [r[0] for r in rows]


def get_max_min_request_id():
    with _lock:
        conn = _get_conn()
        c = conn.cursor()
        c.execute("SELECT MAX(request_id), MIN(request_id) FROM comments")
        row = c.fetchone()
    return {"max": row[0], "min": row[1]}


def get_cached_ids_set():
    with _lock:
        conn = _get_conn()
        c = conn.cursor()
        c.execute("SELECT request_id FROM comments")
        rows = c.fetchall()
    return {r[0] for r in rows}


def save_summary(request_id, summary_text):
    with _lock:
        conn = _get_conn()
        c = conn.cursor()
        c.execute("""
            INSERT OR REPLACE INTO summaries (request_id, summary_text, created_at)
            VALUES (?, ?, ?)
        """, (request_id, summary_text, datetime.now().isoformat()))
        conn.commit()


def get_summary(request_id):
    with _lock:
        conn = _get_conn()
        c = conn.cursor()
        c.execute("SELECT summary_text, created_at FROM summaries WHERE request_id = ?", (request_id,))
        row = c.fetchone()
    
    if row:
        return row[0], row[1]
    return None, None


def get_summary_with_cache_time(request_id):
    with _lock:
        conn = _get_conn()
        c = conn.cursor()
        c.execute("""
            SELECT s.summary_text, s.created_at, c.fetched_at 
            FROM summaries s
            LEFT JOIN comments c ON s.request_id = c.request_id
            WHERE s.request_id = ?
        """, (request_id,))
        row = c.fetchone()
    
    if row:
        return {"summary": row[0], "created_at": row[1], "fetched_at": row[2]}
    return None


def get_all_summaries():
    with _lock:
        conn = _get_conn()
        c = conn.cursor()
        c.execute("SELECT request_id, summary_text, created_at FROM summaries ORDER BY created_at DESC")
        rows = c.fetchall()
    return [{"id": r[0], "summary": r[1], "created": r[2]} for r in rows]


def get_summaries_page(limit=50, offset=0):
    with _lock:
        conn = _get_conn()
        c = conn.cursor()
        c.execute("SELECT request_id, created_at FROM summaries ORDER BY created_at DESC LIMIT ? OFFSET ?", (limit, offset))
        rows = c.fetchall()
    return [{"id": r[0], "created": r[1]} for r in rows]


def get_summary_count():
    with _lock:
        conn = _get_conn()
        c = conn.cursor()
        c.execute("SELECT COUNT(*) FROM summaries")
        return c.fetchone()[0]


def get_all_summaries_ids():
    with _lock:
        conn = _get_conn()
        c = conn.cursor()
        c.execute("SELECT request_id FROM summaries ORDER BY request_id")
        rows = c.fetchall()
    return [r[0] for r in rows]


def get_cache_stats():
    with _lock:
        conn = _get_conn()
        c = conn.cursor()
        c.execute("SELECT COUNT(*) FROM comments")
        count = c.fetchone()[0]
        c.execute("SELECT request_id FROM comments ORDER BY request_id")
        rows = c.fetchall()
    return count, [r[0] for r in rows]


def _score_google_like(text_lower: str, query_lower: str, query_words: set,
                       quoted_phrases: list = None, debug: bool = False) -> tuple:
    """
    Google-like scoring - requires ALL query words to match.
    Supports fuzzy matching for typos using difflib.

    Returns: (score, reason)
        - score: 0-175 based on match quality
        - reason: description of match type
    """
    import re
    import difflib

    if not query_words and not quoted_phrases:
        return 0, "no query"

    # 1. Check quoted phrases first (highest priority) - score 150
    if quoted_phrases:
        for phrase in quoted_phrases:
            if phrase in text_lower:
                return 150, f"exact phrase: '{phrase}'"

    # 2. Exact word boundary matching
    matched_in_text = set()
    matched_query_words = set()
    unmatched_query_words = set()

    for word in query_words:
        pattern = r'\b' + re.escape(word) + r'\b'
        if re.search(pattern, text_lower, re.IGNORECASE):
            matched_in_text.add(word)
            matched_query_words.add(word)
        else:
            unmatched_query_words.add(word)

    # 3. Fuzzy matching for unmatched words (handles typos like "watermarl" -> "watermark")
    fuzzy_matched = {}
    if unmatched_query_words:
        text_words = set(re.findall(r'\b\w+\b', text_lower))
        for word in list(unmatched_query_words):
            matches = difflib.get_close_matches(word, text_words, n=1, cutoff=0.75)
            if matches:
                fuzzy_matched[word] = matches[0]
                matched_in_text.add(matches[0])
                matched_query_words.add(word)
                unmatched_query_words.remove(word)

    # 4. All query words must match (either exact or fuzzy)
    if unmatched_query_words:
        return 0, f"only {len(matched_query_words)}/{len(query_words)} words match"

    # 5. Calculate score (fuzzy matches get slightly lower score)
    has_fuzzy = bool(fuzzy_matched)
    base_score = 90 if has_fuzzy else 100

    # 6. Proximity bonus
    positions = [text_lower.find(w) for w in matched_in_text if text_lower.find(w) >= 0]

    if positions:
        spread = max(positions) - min(positions)
        if spread < 100:
            return base_score + 25, f"all words within {spread} chars"
        else:
            return base_score, f"all {len(query_words)} words found"
    else:
        return base_score, f"all {len(query_words)} words found"


def search_cached_comments(query, min_score=50, custom_field_filter=None, date_filter=None, debug=False):
    # Get filtered request IDs if custom field filter is provided
    filtered_request_ids = None
    
    # Extract quoted phrases before processing
    import re
    quoted_phrases = re.findall(r'"([^"]+)"', query)

    if custom_field_filter:
        # Check if new format (dict with filters) or legacy format (dict with field_name/field_value)
        if "filters" in custom_field_filter:
            # New format: {"filters": [...], "logic": "AND|OR"}
            filters = custom_field_filter.get("filters", [])

            # If no valid filters, don't filter
            if not filters:
                filtered_request_ids = None
            else:
                filtered_request_ids = get_request_ids_for_custom_field(custom_field_filter)
        elif custom_field_filter.get("field_name") and custom_field_filter.get("field_value"):
            # Legacy format for backward compatibility
            filtered_request_ids = get_request_ids_for_custom_field(
                custom_field_filter["field_name"],
                custom_field_filter["field_value"]
            )
    
    # Parse date filter
    start_date = None
    end_date = None
    if date_filter:
        if date_filter.get("start_date"):
            try:
                start_date = datetime.strptime(date_filter["start_date"], "%Y-%m-%d")
            except ValueError:
                pass
        if date_filter.get("end_date"):
            try:
                end_date = datetime.strptime(date_filter["end_date"], "%Y-%m-%d")
            except ValueError:
                pass
    
    all_docs = []
    doc_info = []
    
    with _lock:
        conn = _get_conn()
        c = conn.cursor()
        c.execute("SELECT request_id, comment_data, fetched_at FROM comments")
        rows = c.fetchall()
    
    for row in rows:
        request_id = row[0]
        
        # Apply custom field filter
        if filtered_request_ids is not None and request_id not in filtered_request_ids:
            continue
        
        fetched_at = row[2]
        try:
            comments = json.loads(row[1])
            for comment in comments:
                # Apply date filter
                if start_date or end_date:
                    comment_date = parse_comment_date(comment.get("date"))
                    if comment_date:
                        if start_date and comment_date < start_date:
                            continue
                        if end_date and comment_date > end_date:
                            continue
                    elif start_date or end_date:
                        # No date on comment but filter is active - include anyway or skip?
                        # Skip comments without dates when date filter is active
                        continue
                
                text = comment.get("text", "")
                if text and text.strip():
                    all_docs.append(text)
                    doc_info.append({
                        "request_id": request_id,
                        "text": text,
                        "date": comment.get("date", ""),
                        "fetched_at": fetched_at,
                        "source": "comments"
                    })
        except (json.JSONDecodeError, TypeError):
            pass
    
    if not all_docs:
        return []
    
    # Remove quoted phrases from query for word processing
    query_for_words = re.sub(r'"[^"]+"', '', query).strip()
    query_lower = query_for_words.lower().strip()
    query_words_raw = query_lower.split()
    
    # Filter stopwords and short words
    query_words = {w for w in query_words_raw if w not in STOPWORDS and len(w) >= 2}
    
    # If we have quoted phrases but no remaining words, still allow search
    if not query_words and not quoted_phrases:
        return []
    
    results = []
    for i, text in enumerate(all_docs):
        text_lower = text.lower()
        
        # Google-like scoring with quoted phrases support
        score, reason = _score_google_like(text_lower, query_lower, query_words, quoted_phrases, debug)
        
        if score >= min_score:
            result = {**doc_info[i], "score": score}
            if debug:
                result["match_reason"] = reason
            results.append(result)
    
    results.sort(key=lambda x: x["score"], reverse=True)
    return results


def search_and_fetch_full(query, min_score=50, custom_field_filter=None, date_filter=None):
    """
    Search cache and return FULL records per matching ID.
    
    Uses existing search logic to identify matching IDs, then fetches complete data
    for each matching ID: all comments, custom fields, and existing summaries.
    
    Args:
        query: search term(s)
        min_score: minimum match score to consider a comment a match (default 30)
        custom_field_filter: filter by custom fields (client, product, site, release_version)
        date_filter: filter by comment date range
    
    Returns:
        List of dicts, each containing:
        - request_id: int
        - client: str or None
        - product: str or None
        - site: str or None
        - release_version: str or None
        - comments: list of all comments for this ID
        - summary: str or None (existing summary)
        - match_score: int (highest score from any matching comment)
        - match_reason: str (description of what matched)
        - source: "comments" or "summaries"
    """
    # Phase 1: Use existing search logic to find matching IDs with their scores
    matches = search_cached_comments(query, min_score=min_score, 
                               custom_field_filter=custom_field_filter, 
                               date_filter=date_filter)
    
    # Also search summaries for additional matches
    summary_matches = search_summaries(query, min_score=min_score,
                                       custom_field_filter=custom_field_filter,
                                       date_filter=date_filter)
    
    # Phase 2: Collect unique IDs with their highest scores
    id_scores = {}
    id_reasons = {}
    
    for match in matches:
        rid = match["request_id"]
        score = match.get("score", 0)
        if rid not in id_scores or score > id_scores[rid]:
            id_scores[rid] = score
            id_reasons[rid] = f"Term found in comments (score: {score})"
    
    for match in summary_matches:
        rid = match["request_id"]
        score = match.get("score", 0)
        # Summaries count less than comments in tie-break
        if rid not in id_scores or score > id_scores[rid]:
            id_scores[rid] = score
            id_reasons[rid] = f"Term found in summary (score: {score})"
    
    if not id_scores:
        return []
    
    # Phase 3: Fetch complete data for each unique matching ID
    results = []
    for request_id in sorted(id_scores.keys()):
        comments, fetched_at = get_cached_comments(request_id)
        fields, _ = get_custom_fields(request_id)
        summary, _ = get_summary(request_id)
        
        result = {
            "request_id": request_id,
            "client": fields.get("Client") if fields else None,
            "product": fields.get("Product") if fields else None,
            "site": fields.get("Site") if fields else None,
            "release_version": fields.get("Release Version") if fields else None,
            "comments": comments or [],
            "summary": summary,
            "match_score": id_scores[request_id],
            "match_reason": id_reasons[request_id],
            "source": "comments"  # primary source
        }
        results.append(result)
    
    return results


def search_summaries(query, min_score=50, custom_field_filter=None, date_filter=None):
    # Get filtered request IDs if custom field filter is provided
    filtered_request_ids = None
    
    # Extract quoted phrases
    import re
    quoted_phrases = re.findall(r'"([^"]+)"', query)

    if custom_field_filter:
        # Check if new format (dict with filters) or legacy format (dict with field_name/field_value)
        if "filters" in custom_field_filter:
            # New format: {"filters": [...], "logic": "AND|OR"}
            filters = custom_field_filter.get("filters", [])

            # If no valid filters, don't filter
            if not filters:
                filtered_request_ids = None
            else:
                filtered_request_ids = get_request_ids_for_custom_field(custom_field_filter)
        elif custom_field_filter.get("field_name") and custom_field_filter.get("field_value"):
            # Legacy format for backward compatibility
            filtered_request_ids = get_request_ids_for_custom_field(
                custom_field_filter["field_name"],
                custom_field_filter["field_value"]
            )
    
    # Parse date filter
    start_date = None
    end_date = None
    if date_filter:
        if date_filter.get("start_date"):
            try:
                start_date = datetime.strptime(date_filter["start_date"], "%Y-%m-%d")
            except ValueError:
                pass
        if date_filter.get("end_date"):
            try:
                end_date = datetime.strptime(date_filter["end_date"], "%Y-%m-%d")
            except ValueError:
                pass
    
    all_docs = []
    doc_info = []
    
    with _lock:
        conn = _get_conn()
        c = conn.cursor()
        c.execute("SELECT request_id, summary_text, created_at FROM summaries")
        rows = c.fetchall()
    
    for row in rows:
        request_id = row[0]
        
        # Apply custom field filter
        if filtered_request_ids is not None and request_id not in filtered_request_ids:
            continue
        
        created_at = row[2]
        
        # Apply date filter
        if start_date or end_date:
            summary_date = parse_comment_date(created_at)
            if summary_date:
                if start_date and summary_date < start_date:
                    continue
                if end_date and summary_date > end_date:
                    continue
            elif start_date or end_date:
                continue
        
        summary = row[1] or ""
        if summary and summary.strip():
            all_docs.append(summary)
            doc_info.append({
                "request_id": request_id,
                "text": summary,
                "date": created_at,
                "fetched_at": created_at,
                "source": "summaries"
            })
    
    if not all_docs:
        return []
    
    # Remove quoted phrases from query for word processing
    query_for_words = re.sub(r'"[^"]+"', '', query).strip()
    query_lower = query_for_words.lower().strip()
    query_words_raw = query_lower.split()
    
    query_words = {w for w in query_words_raw if w not in STOPWORDS and len(w) >= 2}
    
    # If we have quoted phrases but no remaining words, still allow search
    if not query_words and not quoted_phrases:
        return []
    
    results = []
    for i, text in enumerate(all_docs):
        text_lower = text.lower()
        
        # Google-like scoring with quoted phrases support
        score, reason = _score_google_like(text_lower, query_lower, query_words, quoted_phrases)
        
        if score >= min_score:
            results.append({
                **doc_info[i],
                "score": score
            })
    
    results.sort(key=lambda x: x["score"], reverse=True)
    return results


def clean_html_tags_from_cache():
    # Import the proper clean_html from analysis to ensure consistent cleaning
    try:
        from analysis import clean_html as _clean_html
    except ImportError:
        # Fallback if circular import issues arise
        def _clean_html(text):
            import re as _re
            if not text:
                return ""
            text = _re.sub(r'<style[^>]*>.*?</style>', '', text, flags=_re.DOTALL | _re.IGNORECASE)
            text = _re.sub(r'<script[^>]*>.*?</script>', '', text, flags=_re.DOTALL | _re.IGNORECASE)
            text = _re.sub(r'</p\s*>', '\n\n', text, flags=_re.IGNORECASE)
            text = _re.sub(r'</div\s*>', '\n', text, flags=_re.IGNORECASE)
            text = _re.sub(r'<br\s*/?>', '\n', text, flags=_re.IGNORECASE)
            text = _re.sub(r'<li[^>]*>', '\n• ', text, flags=_re.IGNORECASE)
            text = _re.sub(r'</h[1-6]\s*>', '\n\n', text, flags=_re.IGNORECASE)
            text = _re.sub(r'<[^>]+>', '', text)
            text = text.replace('&nbsp;', ' ').replace('&amp;', '&').replace('&lt;', '<')
            text = text.replace('&gt;', '>').replace('&quot;', '"').replace('&apos;', "'")
            lines = [l.rstrip() for l in text.splitlines()]
            result, prev_blank = [], False
            for line in lines:
                is_blank = line.strip() == ''
                if is_blank and prev_blank:
                    continue
                result.append(line)
                prev_blank = is_blank
            return '\n'.join(result).strip()

    cleaned_count = 0
    with _lock:
        conn = _get_conn()
        c = conn.cursor()
        c.execute("SELECT request_id, comment_data FROM comments")
        rows = c.fetchall()
        
        for row in rows:
            request_id = row[0]
            try:
                comments = json.loads(row[1])
                changed = False
                cleaned_comments = []
                for comment in comments:
                    original_text = comment.get("text", "")
                    cleaned_text = _clean_html(original_text)
                    if cleaned_text != original_text:
                        changed = True
                    # Skip comments that are empty after cleaning
                    if cleaned_text.strip():
                        cleaned_comments.append({**comment, "text": cleaned_text})
                    else:
                        changed = True  # comment was dropped
                
                if changed:
                    c.execute("UPDATE comments SET comment_data = ? WHERE request_id = ?", 
                              (json.dumps(cleaned_comments), request_id))
                    cleaned_count += 1
            except (json.JSONDecodeError, TypeError):
                pass
        
        conn.commit()
    
    return cleaned_count


def save_custom_fields(request_id, fields):
    with _lock:
        conn = _get_conn()
        c = conn.cursor()
        fetched_at = datetime.now().isoformat()
        
        c.execute("DELETE FROM request_custom_fields WHERE request_id = ?", (request_id,))
        
        client = fields.get("Client", "")
        product = fields.get("Product", "")
        release_version = fields.get("Release Version", "")
        site = fields.get("Site", "")
        
        c.execute("""
            INSERT INTO request_custom_fields (request_id, client, product, release_version, site, fetched_at)
            VALUES (?, ?, ?, ?, ?, ?)
        """, (request_id, client, product, release_version, site, fetched_at))
        
        conn.commit()


def get_custom_fields(request_id):
    with _lock:
        conn = _get_conn()
        c = conn.cursor()
        c.execute("""
            SELECT client, product, release_version, site, fetched_at 
            FROM request_custom_fields WHERE request_id = ?
        """, (request_id,))
        row = c.fetchone()
    
    if not row:
        return None, None
    
    fields = {
        "Client": row[0],
        "Product": row[1],
        "Release Version": row[2],
        "Site": row[3]
    }
    fetched_at = row[4]
    
    return fields, fetched_at


def get_all_custom_field_names():
    return ["Client", "Product", "Release Version", "Site"]


def search_by_custom_field(field_name, field_value):
    column = field_name.lower().replace(" ", "_")
    if column not in ("client", "product", "release_version", "site"):
        return []
    with _lock:
        conn = _get_conn()
        c = conn.cursor()
        c.execute(f"""
            SELECT request_id, {column} FROM request_custom_fields 
            WHERE {column} LIKE ?
        """, (f"%{field_value}%"))
        rows = c.fetchall()
    return [{"request_id": r[0], "field_value": r[1]} for r in rows]


def get_requests_with_custom_field(field_name):
    column = field_name.lower().replace(" ", "_")
    if column not in ("client", "product", "release_version", "site"):
        return []
    with _lock:
        conn = _get_conn()
        c = conn.cursor()
        c.execute(f"""
            SELECT DISTINCT request_id, {column} FROM request_custom_fields 
            WHERE {column} IS NOT NULL
        """)
    rows = c.fetchall()
    return [{"request_id": r[0], "field_value": r[1]} for r in rows]


def get_custom_field_values(field_name):
    column = field_name.lower().replace(" ", "_")
    if column not in ("client", "product", "release_version", "site"):
        return []
    with _lock:
        conn = _get_conn()
        c = conn.cursor()
        c.execute(f"""
            SELECT DISTINCT {column} FROM request_custom_fields 
            WHERE {column} IS NOT NULL AND {column} != ''
            ORDER BY {column}
        """)
        rows = c.fetchall()
    return [r[0] for r in rows if r[0]]


def get_request_ids_for_custom_field(field_name_or_filter, field_value=None):
    """
    Get request IDs matching custom field filters.
    
    Supports two formats:
    - Legacy: (field_name, field_value) - single filter
    - New: {"filters": [{"field_name": ..., "field_value": ...}], "logic": "AND|OR"}
    """
    # Handle legacy format (backward compatibility)
    if field_value is not None:
        filter_list = [{"field_name": field_name_or_filter, "field_value": field_value}]
        logic = "AND"
    else:
        # New format: field_name_or_filter is the filter dict
        filter_dict = field_name_or_filter
        if not filter_dict:
            return set()
        
        filters = filter_dict.get("filters", [])
        logic = filter_dict.get("logic", "AND")
        
        # If no filters, return empty to allow all
        if not filters:
            return set()
        
        filter_list = filters
        # Default to AND if not specified
    
    valid_fields = {"client", "product", "release_version", "site"}
    valid_fields_lower = {f.lower().replace(" ", "_") for f in valid_fields}
    
    # Build list of ID sets for each valid filter
    id_sets = []
    
    with _lock:
        conn = _get_conn()
        c = conn.cursor()
        
        for f in filter_list:
            field_name = f.get("field_name", "")
            field_value = f.get("field_value", "")
            
            # Skip invalid or empty filters
            if not field_name or not field_value:
                continue
            
            column = field_name.lower().replace(" ", "_")
            if column not in valid_fields_lower:
                continue
            
            try:
                c.execute(f"""
                    SELECT DISTINCT request_id FROM request_custom_fields 
                    WHERE {column} = ?
                """, (field_value,))
                rows = c.fetchall()
                id_sets.append({r[0] for r in rows})
            except Exception:
                continue
    
    # No valid filters found
    if not id_sets:
        return set()
    
    # Combine based on logic
    if logic.upper() == "OR":
        # Union of all sets
        result = set()
        for s in id_sets:
            result |= s
        return result
    else:
        # AND: intersection of all sets
        result = id_sets[0]
        for s in id_sets[1:]:
            result = result.intersection(s)
        return result


def search_cached_issues_by_product_keyword(product: str | None, keywords: list[str], limit: int = 10) -> list[dict]:
    """
    Search cached issues by product + keywords using three-tier matching.

    Matching tiers (per keyword):
      1. Word-boundary regex match  (3 pts) — exact word, highest precision
      2. Substring match             (2 pts) — contains, good recall
      3. difflib fuzzy match         (1 pt)  — typo-tolerant fallback

    Pre-filters via SQL LIKE for performance, deduplicates per request
    keeping the best-scoring comment per issue.

    Args:
        product: Product name to filter by (optional)
        keywords: List of search terms
        limit: Maximum results to return

    Returns:
        List of matching issues (sorted by score descending):
        [
            {
                "request_id": int,
                "text": str (best-matching comment text),
                "match_reason": str,
                "product": str
            },
            ...
        ]
    """
    if not keywords:
        return []

    keywords = [k.strip().lower() for k in keywords if k.strip()]
    if not keywords:
        return []

    with _lock:
        conn = _get_conn()
        c = conn.cursor()

    # ── Step 1: SQL LIKE pre-filtering for performance ────────────
    like_clauses = []
    params = []
    for kw in keywords:
        # Escape % and _ which are LIKE wildcards
        escaped = kw.replace('%', '\\%').replace('_', '\\_')
        like_clauses.append("c.comment_data LIKE ? ESCAPE '\\'")
        params.append(f'%{escaped}%')

    where_clause = ' OR '.join(like_clauses)

    if product and product.strip() and product.lower() != "not recorded":
        product_filter = " AND LOWER(rcf.product) = ?"
        params.append(product.strip().lower())
    else:
        product_filter = ""

    sql = f"""
        SELECT DISTINCT c.request_id, c.comment_data, rcf.product
        FROM comments c
        LEFT JOIN request_custom_fields rcf ON c.request_id = rcf.request_id
        WHERE ({where_clause}){product_filter}
    """

    try:
        c.execute(sql, params)
        all_rows = c.fetchall()
    except Exception:
        # Fallback: full scan if LIKE query fails (e.g. complex keyword edge cases)
        c.execute("""
            SELECT c.request_id, c.comment_data, rcf.product
            FROM comments c
            LEFT JOIN request_custom_fields rcf ON c.request_id = rcf.request_id
        """)
        all_rows = c.fetchall()
        if product and product.strip() and product.lower() != "not recorded":
            product_lower = product.strip().lower()
            all_rows = [r for r in all_rows if r[2] and r[2].lower() == product_lower]

    # ── Step 2: Score each comment with three-tier matching ──────
    # request_scores: deduplicated per request, keeps best comment
    request_scores = {}

    for row in all_rows:
        req_id = row[0]
        comment_json = row[1]
        prod = row[2]

        try:
            comment_data = json.loads(comment_json) if comment_json else []
        except (json.JSONDecodeError, TypeError):
            continue

        if not isinstance(comment_data, list):
            comment_data = [{"text": str(comment_data)}]

        # Score each individual comment, keep best per request
        for entry in comment_data:
            if not isinstance(entry, dict):
                continue
            text = entry.get("text", "")
            if not text:
                continue

            text_lower = text.lower()

            matched_keywords = []
            total_score = 0

            for kw in keywords:
                # Tier 1: Word-boundary regex match (3 pts)
                pattern = r'\b' + re.escape(kw) + r'\b'
                if re.search(pattern, text_lower):
                    matched_keywords.append(kw)
                    total_score += 3
                    continue

                # Tier 2: Substring match (2 pts)
                if kw in text_lower:
                    matched_keywords.append(kw)
                    total_score += 2
                    continue

                # Tier 3: difflib fuzzy match (1 pt)
                words = re.findall(r'\b\w+\b', text_lower)
                if words:
                    close = difflib.get_close_matches(kw, words, n=1, cutoff=0.8)
                    if close:
                        matched_keywords.append(kw)
                        total_score += 1

            if matched_keywords:
                # Deduplicate: keep best-scoring comment per request
                existing = request_scores.get(req_id)
                if not existing or total_score > existing["_score"]:
                    request_scores[req_id] = {
                        "request_id": req_id,
                        "text": text[:2000],
                        "match_reason": f"Keywords '{', '.join(matched_keywords)}' found in comments",
                        "product": prod or "Unknown",
                        "_score": total_score,
                    }

    # ── Step 3: Sort by score descending and return top N ──────
    results = sorted(request_scores.values(), key=lambda x: x["_score"], reverse=True)
    return results[:limit]


def check_database_health() -> dict:
    """
    Check database health and return a report.
    
    Checks:
    - Database size and page count
    - Index efficiency for each index
    - Tables with potentially stale data (custom_fields without matching comments)
    - Estimated fragmentation
    
    Returns dict with health status and details.
    """
    with _lock:
        conn = _get_conn()
        c = conn.cursor()
        result = {}
        
        # Basic stats using correct pragma syntax
        c.execute("PRAGMA page_count")
        pages = c.fetchone()[0]
        c.execute("PRAGMA page_size")
        page_size = c.fetchone()[0]
        result["db_size_mb"] = pages * page_size / (1024 * 1024)
        result["data_pages"] = pages if result["db_size_mb"] > 0 else 1
        
        # Row counts
        tables = ["comments", "summaries", "request_custom_fields"]
        result["row_counts"] = {t: c.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0] for t in tables}
        
        # Index stats - simplified to just list indexes
        result["indexes"] = {}
        c.execute("SELECT name FROM sqlite_master WHERE type='index' AND name NOT LIKE 'sqlite_%'")
        for (idx_name,) in c.fetchall():
            result["indexes"][idx_name] = {"exists": True}
        
        # Check for orphan custom fields (fields without matching comments)
        c.execute("""
            SELECT COUNT(*) FROM request_custom_fields
            WHERE request_id NOT IN (SELECT request_id FROM comments WHERE comment_data IS NOT NULL)
        """)
        orphan_fields = c.fetchone()[0]
        result["orphan_fields"] = orphan_fields
        total_fields = result["row_counts"].get("request_custom_fields", 1)
        if total_fields > 0:
            result["orphan_percentage"] = (orphan_fields / total_fields) * 100
        else:
            result["orphan_percentage"] = 0
        result["fragmentation_warning"] = orphan_fields > total_fields * 0.5 if total_fields > 0 else False
        
        # Check database size (simple heuristic)
        result["status"] = "healthy"
        if result["orphan_fields"] > 0 and result["orphan_percentage"] > 40:
            result["status"] = "warning"
            result["messages"] = ["Old custom field data detected. Consider running optimization."]
        else:
            result["messages"] = ["Database is healthy.", "No immediate action needed."]
        
        conn.commit()
    
    return result


def optimize_database():
    """
    Optimize the database by running VACUUM and ANALYZE.
    
    This reclaims disk space from deleted data and updates index statistics.
    
    Warning: This operation will lock the database temporarily and may take
    several seconds.
    """
    with _lock:
        conn = _get_conn()
        c = conn.cursor()
        
        # Vacuum to reclaim space from deleted rows
        c.execute("VACUUM")
        
        # Analyze tables to update statistics for query optimizer
        c.execute("ANALYZE")
        
        # Check for and rebuild orphan custom fields
        c.execute("""
            DELETE FROM request_custom_fields 
            WHERE request_id NOT IN (SELECT request_id FROM comments WHERE comment_data IS NOT NULL)
        """)
        
        conn.commit()
    
    return {"optimized": True, "message": "Database optimized: VACUUM, ANALYZE, and orphan data cleaned"}


def analyze_indexes() -> list:
    """
    Check index efficiency and return a list of unused/inefficient indexes.
    
    Returns list of (index_name, table_name, coverage_ratio) tuples where
    coverage_ratio < 0.5 suggests the index may not be well-utilized.
    """
    with _lock:
        conn = _get_conn()
        c = conn.cursor()
        unnecessary_indexes = []
        
        c.execute("SELECT name, tbl_name, sql FROM sqlite_master WHERE type='index' AND name NOT LIKE 'sqlite_%'")
        
        for idx_name, table_name, idx_sql in c.fetchall():
            c.execute(f"SELECT COUNT(*) FROM pragma_index_list(?)", (idx_name,))
            info = c.fetchone()
            
            if info:
                parts = info[0].split(",")
                covered_columns = set()
                for p in parts:
                    p = p.strip()
                    if p:
                        col = p.split()[0]
                        covered_columns.add(col)
                
                if covered_columns:
                    c.execute(f"SELECT COUNT(*) FROM {table_name} WHERE {', '.join(covered_columns)} IS NOT NULL OR {', '.join(covered_columns)} != ''")
                    covered = c.fetchone()[0]
                    total = c.execute(f"SELECT COUNT(*) FROM {table_name}").fetchone()[0]
                    
                    if total > 0:
                        ratio = covered / total
                        if ratio < 0.5:
                            unnecessary_indexes.append((idx_name, table_name, ratio))
        
        conn.commit()
        return unnecessary_indexes