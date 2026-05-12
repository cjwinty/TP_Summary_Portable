import json
import requests
import sys
import os
import certifi
import re
import logging
from datetime import datetime
from config import BASE_URL, USERNAME, PASSWORD, PROJECT_NAME
from database import get_cached_comments, save_comments, save_custom_fields
from analysis import clean_html

logger = logging.getLogger(__name__)

# Resolve CA bundle path — certifi.where() uses importlib.resources which
# can fail in PyInstaller frozen environments. Search likely locations.
def _resolve_ca_bundle():
    if not getattr(sys, 'frozen', False):
        return certifi.where()
    for candidate in (
        certifi.where(),
        os.path.join(os.path.dirname(sys.executable), '_internal', 'cacert.pem'),
        os.path.join(os.path.dirname(sys.executable), '_internal', 'certifi', 'cacert.pem'),
        os.path.join(os.path.dirname(sys.executable), 'cacert.pem'),
    ):
        if os.path.exists(candidate):
            return candidate
    return certifi.where()  # let it fail naturally

CA_BUNDLE = _resolve_ca_bundle()


def make_request(url, params, retries=3):
    for attempt in range(retries):
        try:
            r = requests.get(
                url, params=params, auth=(USERNAME, PASSWORD),
                timeout=30, verify=CA_BUNDLE,
            )
            try:
                data = r.json()
            except ValueError:
                snippet = (r.text or r.content or b"").decode("utf-8", errors="replace")[:200]
                logger.warning(
                    "API returned non-JSON response (HTTP %d): %s",
                    r.status_code, snippet,
                )
                if attempt == retries - 1:
                    return None
                continue
            if "Status" in data and data["Status"] == "BadRequest":
                logger.error("API Error: %s", data.get("Message", "Unknown"))
                return None
            return data
        except requests.RequestException as e:
            logger.warning("Request failed (attempt %d/%d): %s", attempt + 1, retries, e)
            if attempt == retries - 1:
                return None
    return None


def get_requests():
    url = f"{BASE_URL}/Requests"
    params = {
        "where": f'Project.Name = "{PROJECT_NAME}"',
        "take": 200
    }
    data = make_request(url, params)
    return data.get("items", []) if data else []


def get_comments(request_id, use_cache=True):
    if use_cache:
        cached, fetched_at = get_cached_comments(request_id)
        if cached is not None:
            return cached, fetched_at, False
    
    all_comments = []
    api_error = False
    
    comments_url = f"{BASE_URL}/Comments"
    dates_url = f"{BASE_URL}/Comments"
    comments_params = {"where": f"General.Id = {request_id}", "select": "Description", "take": 100}
    dates_params = {"where": f"General.Id = {request_id}", "select": "CreateDate", "take": 100}
    
    comments_by_idx = {}
    dates_by_idx = {}
    max_idx = 0
    
    while comments_url:
        data = make_request(comments_url, comments_params)
        if not data:
            api_error = True
            break
        items = data.get("items", [])
        for i, item in enumerate(items):
            if isinstance(item, str) and item.strip():
                comments_by_idx[max_idx + i] = item
        max_idx += len(items)
        comments_url = data.get("next")
        comments_params = {}
    
    max_idx = 0
    while dates_url:
        data = make_request(dates_url, dates_params)
        if not data:
            break
        items = data.get("items", [])
        for i, item in enumerate(items):
            if isinstance(item, str) and item.strip():
                dates_by_idx[max_idx + i] = item
        max_idx += len(items)
        dates_url = data.get("next")
        dates_params = {}
    
    for idx in sorted(comments_by_idx.keys()):
        raw_text = comments_by_idx[idx]
        cleaned_text = clean_html(raw_text)
        # Only store non-empty comments
        if cleaned_text.strip():
            all_comments.append({"text": cleaned_text, "date": dates_by_idx.get(idx)})
    
    all_comments.sort(key=lambda x: x.get("date") or "")
    
    if api_error and not all_comments:
        return None, None, True

    if all_comments:
        save_comments(request_id, all_comments)
        
        custom_fields = get_custom_fields_from_request(request_id)
        if custom_fields:
            save_custom_fields(request_id, custom_fields)

    fetched_at = datetime.now().isoformat()
    return all_comments, fetched_at, True


def get_custom_fields_from_request(request_id):
    v1_base = BASE_URL.replace("/api/v2", "/api/v1")
    url = f"{v1_base}/requests/{request_id}"
    params = {
        "skip": 0,
        "take": 1,
        "include": "[id,customFields]"
    }
    
    for attempt in range(3):
        try:
            r = requests.get(
                url, params=params, auth=(USERNAME, PASSWORD),
                timeout=30, verify=CA_BUNDLE,
            )
            if r.status_code != 200:
                logger.warning("Custom fields API returned status %d for request %s", r.status_code, request_id)
                return None
            
            custom_fields_xml = r.text
            
            start = custom_fields_xml.find("<CustomFields>")
            if start < 0:
                return None
            
            cf_section = custom_fields_xml[start:]
            
            fields = {}
            field_pattern = r'<Field\s+Type="([^"]*)">\s*<Name>([^<]*)</Name>\s*<Value>([^<]*)</Value>\s*</Field>'
            matches = re.findall(field_pattern, cf_section, re.DOTALL)
            
            for field_type, field_name, field_value in matches:
                if field_value and field_value.strip() and field_value.strip() != "true":
                    fields[field_name] = field_value.strip()
                elif field_value == "true":
                    fields[field_name] = "Yes"
            
            EXCLUDE_FIELDS = {'Out of hours', 'Next Action', 'Stop Feedback Request', 
                              'Internal Priority', 'Paid Work', 'Support Level', 'Downtime',
                              'CustomerRef'}
            fields = {k: v for k, v in fields.items() if k not in EXCLUDE_FIELDS}
            
            return fields if fields else None
        except requests.RequestException as e:
            if attempt == 2:
                return None
        except Exception as e:
            return None
    
    return None