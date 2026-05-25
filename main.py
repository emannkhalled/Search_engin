"""
Mini Search Engine - University IR Project
FastAPI backend with Elasticsearch integration.

Run:
    uvicorn main:app --reload
Open:
    http://127.0.0.1:8000
"""

import os
import sys
import json
import datetime
import traceback
import hashlib
from typing import List, Optional, Dict, Any

import pandas as pd
import fitz  # PyMuPDF

from fastapi import FastAPI, Query, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from elasticsearch import Elasticsearch, helpers, NotFoundError

# =============================================================================
# Elasticsearch connection
# =============================================================================
ES_HOST = "https://my-elasticsearch-project-dff93f.es.us-central1.gcp.elastic.cloud:443"
ES_API_KEY = "MWUyR0NKNEJ0d1ktQzMycGszQ0s6V2EwQkxTbWl4dEIzMTJBeGZaYUc1UQ=="
INDEX_NAME = "mini_search_engine"

print("=" * 60)
print("[INFO] Starting Mini Search Engine...")
print(f"[INFO] ES_HOST: {ES_HOST}")
print("=" * 60)

es = None
connection_error_msg = ""

try:
    es = Elasticsearch(
        hosts=[ES_HOST],
        api_key=ES_API_KEY,
        request_timeout=60,
        verify_certs=True,
    )
    info = es.info()
    print("[SUCCESS] Connected to Elasticsearch!")
    print(f"[SUCCESS]    Cluster: {info.get('cluster_name', 'unknown')}")
    print(f"[SUCCESS]    Version: {info.get('version', {}).get('number', 'unknown')}")
    print("=" * 60)
except Exception as e:
    connection_error_msg = str(e)
    print(f"[ERROR] Failed to connect to Elasticsearch: {e}")
    print("[ERROR]    The API will still start, but indexing/search won't work.")
    print("=" * 60)

# =============================================================================
# FastAPI app
# =============================================================================
app = FastAPI(title="Mini Search Engine", version="1.0")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# =============================================================================
# Models
# =============================================================================
class IndexRequest(BaseModel):
    folder_path: str
    formats: List[str]


# =============================================================================
# Index mapping - FIXED: Added ignore_above to prevent keyword parsing errors
# =============================================================================
INDEX_MAPPING = {
    "settings": {
        "analysis": {
            "analyzer": {
                "default": {
                    "type": "standard"
                }
            }
        }
    },
    "mappings": {
        "properties": {
            "filename":  {"type": "keyword"},
            "file_type": {"type": "keyword"},
            "content":   {
                "type": "text",
                "fields": {
                    "suggest": {"type": "text"},
                    "keyword": {
                        "type": "keyword",
                        "ignore_above": 32766  # ✅ PREVENTS parsing errors on large content
                    },
                    "terms": {
                        "type": "text",
                        "analyzer": "standard",
                        "fielddata": True
                    }
                }
            },
            "mod_date":  {"type": "date"},
            "file_path": {"type": "keyword"},
        }
    }
}


# =============================================================================
# Helpers - text extraction
# =============================================================================
def extract_txt(path: str) -> List[Dict[str, Any]]:
    try:
        with open(path, "r", encoding="utf-8", errors="ignore") as f:
            text = f.read()
        print(f"[INFO] TXT {path}: {len(text)} chars")
        return [{"content": text}]
    except Exception as e:
        print(f"[WARN] Failed to read TXT {path}: {e}")
        return [{"content": ""}]


def extract_pdf(path: str) -> List[Dict[str, Any]]:
    text_parts = []
    try:
        with fitz.open(path) as doc:
            for page in doc:
                text_parts.append(page.get_text())
        content = "\n".join(text_parts)
        print(f"[INFO] PDF {path}: {len(content)} chars, {len(text_parts)} pages")
        return [{"content": content}]
    except Exception as e:
        print(f"[WARN] Failed to read PDF {path}: {e}")
        return [{"content": ""}]


def _flatten_json_strings(obj: Any, out: List[str]) -> None:
    if isinstance(obj, dict):
        for v in obj.values():
            _flatten_json_strings(v, out)
    elif isinstance(obj, list):
        for v in obj:
            _flatten_json_strings(v, out)
    elif isinstance(obj, str):
        if obj.strip():
            out.append(obj)
    elif isinstance(obj, (int, float, bool)):
        out.append(str(obj))
    elif obj is not None:
        out.append(str(obj))


def extract_json(path: str) -> List[Dict[str, Any]]:
    try:
        with open(path, "r", encoding="utf-8", errors="ignore") as f:
            raw_text = f.read()
        
        try:
            data = json.loads(raw_text)
            parts: List[str] = []
            _flatten_json_strings(data, parts)
            
            if not parts:
                parts = [raw_text]
                print(f"[INFO] JSON {path}: no strings found, using raw JSON ({len(raw_text)} chars)")
            
            content = " ".join(parts)
            print(f"[INFO] JSON {path}: extracted {len(content)} chars, {len(parts)} parts")
            return [{"content": content}]
            
        except json.JSONDecodeError:
            print(f"[INFO] JSON {path}: invalid JSON, using raw text ({len(raw_text)} chars)")
            return [{"content": raw_text}]
            
    except Exception as e:
        print(f"[WARN] Failed to read JSON {path}: {e}")
        return [{"content": ""}]


def extract_csv(path: str) -> List[Dict[str, Any]]:
    docs = []
    try:
        df = pd.read_csv(path, dtype=str, keep_default_na=False, encoding="utf-8")
    except Exception:
        try:
            df = pd.read_csv(path, dtype=str, keep_default_na=False, encoding="latin-1")
        except Exception as e:
            print(f"[WARN] Failed to read CSV {path}: {e}")
            return docs
    
    rows_count = 0
    for _, row in df.iterrows():
        text = " ".join(str(v) for v in row.values if v is not None and str(v).strip())
        if text.strip():
            docs.append({"content": text})
            rows_count += 1
    
    print(f"[INFO] CSV {path}: {rows_count} rows extracted")
    return docs


def extract_xlsx(path: str) -> List[Dict[str, Any]]:
    docs = []
    try:
        sheets = pd.read_excel(path, sheet_name=None, dtype=str)
    except Exception as e:
        print(f"[WARN] Failed to read XLSX {path}: {e}")
        return docs
    
    total_rows = 0
    for sheet_name, df in sheets.items():
        df = df.fillna("")
        sheet_rows = 0
        for _, row in df.iterrows():
            text = " ".join(str(v) for v in row.values if v is not None and str(v).strip())
            if text.strip():
                docs.append({"content": text})
                sheet_rows += 1
                total_rows += 1
        print(f"[INFO] XLSX {path} sheet '{sheet_name}': {sheet_rows} rows")
    
    print(f"[INFO] XLSX {path}: {total_rows} total rows")
    return docs


EXTRACTORS = {
    "txt":  extract_txt,
    "pdf":  extract_pdf,
    "json": extract_json,
    "csv":  extract_csv,
    "xlsx": extract_xlsx,
}


# =============================================================================
# Helpers - indexing
# =============================================================================
def get_file_type(filename: str) -> Optional[str]:
    name = filename.lower()
    if name.endswith(".txt"):  return "txt"
    if name.endswith(".pdf"):  return "pdf"
    if name.endswith(".json"): return "json"
    if name.endswith(".csv"):  return "csv"
    if name.endswith(".xlsx"): return "xlsx"
    return None


def iso_mod_date(path: str) -> str:
    try:
        ts = os.path.getmtime(path)
        dt = datetime.datetime.fromtimestamp(ts)
        return dt.strftime("%Y-%m-%dT%H:%M:%S")
    except Exception as e:
        print(f"[WARN] Failed to get mod date for {path}: {e}")
        return datetime.datetime.now().strftime("%Y-%m-%dT%H:%M:%S")


def recreate_index() -> None:
    if es is None:
        raise HTTPException(
            status_code=503,
            detail=f"Elasticsearch not connected. Error: {connection_error_msg}"
        )
    try:
        exists = es.indices.exists(index=INDEX_NAME)
        if exists:
            print(f"[INFO] Deleting existing index: {INDEX_NAME}")
            es.indices.delete(index=INDEX_NAME)
            print(f"[INFO] Index deleted.")
    except NotFoundError:
        pass
    except Exception as e:
        print(f"[WARN] Error checking/deleting index: {e}")

    print(f"[INFO] Creating index: {INDEX_NAME}")
    try:
        es.indices.create(index=INDEX_NAME, body=INDEX_MAPPING)
        print(f"[SUCCESS] Index created successfully")
    except Exception as e:
        print(f"[ERROR] Failed to create index: {e}")
        raise HTTPException(status_code=500, detail=f"Failed to create index: {str(e)}")


# =============================================================================
# Routes - health
# =============================================================================
@app.get("/health")
def health():
    if es is None:
        return JSONResponse(
            status_code=503,
            content={"status": "error", "detail": f"Elasticsearch not connected: {connection_error_msg}"}
        )
    try:
        info = es.info()
        return {
            "status": "ok",
            "cluster": info.get("cluster_name"),
            "version": info.get("version", {}).get("number")
        }
    except Exception as e:
        return JSONResponse(
            status_code=503,
            content={"status": "error", "detail": str(e)}
        )


# =============================================================================
# Routes - debug: list all files in folder
# =============================================================================
@app.get("/debug/list-files")
def debug_list_files(folder_path: str = Query(..., description="Folder path to scan")):
    """List all files in the folder with their types"""
    if not os.path.isdir(folder_path):
        raise HTTPException(status_code=400, detail=f"Folder does not exist: {folder_path}")
    
    all_files = []
    matched_files = []
    
    for root, dirs, files in os.walk(folder_path):
        for filename in files:
            file_path = os.path.join(root, filename)
            ftype = get_file_type(filename)
            info = {
                "filename": filename,
                "file_path": file_path,
                "file_type": ftype,
                "size_bytes": os.path.getsize(file_path) if os.path.exists(file_path) else 0
            }
            all_files.append(info)
            if ftype:
                matched_files.append(info)
    
    return {
        "folder": folder_path,
        "total_files": len(all_files),
        "matched_files_count": len(matched_files),
        "all_files": all_files,
        "matched_files": matched_files
    }


# =============================================================================
# Routes - debug: list all documents in index
# =============================================================================
@app.get("/debug/list-all")
def debug_list_all():
    """List all documents in the index for debugging"""
    if es is None:
        raise HTTPException(status_code=503, detail="Not connected")
    
    try:
        resp = es.search(
            index=INDEX_NAME,
            body={
                "size": 100,
                "query": {"match_all": {}},
                "_source": ["filename", "file_type", "file_path"]
            }
        )
        
        docs = []
        for h in resp["hits"]["hits"]:
            src = h["_source"]
            docs.append({
                "id": h["_id"],
                "filename": src.get("filename"),
                "file_type": src.get("file_type"),
                "file_path": src.get("file_path")
            })
        
        return {
            "total": resp["hits"]["total"]["value"],
            "documents": docs
        }
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


# =============================================================================
# Routes - indexing - FIXED: Unique IDs + streaming_bulk + detailed logging
# =============================================================================
@app.post("/index")
def build_index(req: IndexRequest):
    print(f"\n{'='*60}")
    print(f"[INFO] Indexing request received")
    print(f"[INFO]   folder: {req.folder_path}")
    print(f"[INFO]   formats: {req.formats}")
    print(f"{'='*60}")

    if es is None:
        print(f"[ERROR] Elasticsearch not connected!")
        raise HTTPException(
            status_code=503,
            detail=f"Elasticsearch not connected. Please check your connection. Error: {connection_error_msg}"
        )

    folder = req.folder_path
    formats = [f.lower().strip() for f in req.formats]

    if not folder or not os.path.isdir(folder):
        print(f"[ERROR] Folder does not exist: {folder}")
        raise HTTPException(status_code=400, detail=f"Folder does not exist: {folder}")

    for fmt in formats:
        if fmt not in EXTRACTORS:
            print(f"[ERROR] Unsupported format: {fmt}")
            raise HTTPException(status_code=400, detail=f"Unsupported format: {fmt}")

    # 1) Recreate index
    try:
        recreate_index()
    except HTTPException:
        raise
    except Exception as e:
        print(f"[ERROR] Failed to recreate index: {e}")
        print(traceback.format_exc())
        raise HTTPException(status_code=500, detail=f"Failed to create index: {str(e)}")

    # 2) Walk folder and extract
    actions: List[Dict[str, Any]] = []
    by_type: Dict[str, int] = {f: 0 for f in formats}
    total = 0
    skipped = 0
    errors = 0

    files_found: Dict[str, int] = {f: 0 for f in formats}
    files_processed: Dict[str, int] = {f: 0 for f in formats}

    for root, _, files in os.walk(folder):
        for filename in files:
            ftype = get_file_type(filename)
            if ftype not in formats:
                print(f"[DEBUG] SKIPPED (format not selected): {filename} (type: {ftype})")
                continue

            files_found[ftype] += 1
            file_path = os.path.join(root, filename)
            print(f"\n[INFO] ==========================================")
            print(f"[INFO] File: {filename} ({ftype})")
            print(f"[INFO] Path: {file_path}")
            print(f"[INFO] Size: {os.path.getsize(file_path)} bytes")

            try:
                mod_date = iso_mod_date(file_path)
            except Exception as e:
                print(f"[WARN] Skipping {filename} - mod_date error: {e}")
                skipped += 1
                continue

            extractor = EXTRACTORS[ftype]
            try:
                docs = extractor(file_path)
                print(f"[INFO] Extracted {len(docs)} document(s) from {filename}")
            except Exception as e:
                print(f"[WARN] Skipping {filename} - extraction error: {e}")
                errors += 1
                continue

            for i, doc in enumerate(docs):
                content = doc.get("content", "") or ""
                content_preview = content[:100].replace("\n", " ") if content else "(empty)"
                print(f"[INFO]   Doc #{i+1}: {len(content)} chars - '{content_preview}...'")

                if not content.strip():
                    print(f"[WARN]   Doc #{i+1} - EMPTY CONTENT, skipping")
                    skipped += 1
                    continue

                # ✅ UNIQUE ID: based on file path + doc index + content length
                doc_id = hashlib.md5(f"{file_path}:{i}:{len(content)}".encode()).hexdigest()
                
                actions.append({
                    "_index": INDEX_NAME,
                    "_id": doc_id,
                    "_source": {
                        "filename":  filename,
                        "file_type": ftype,
                        "content":   content,
                        "mod_date":  mod_date,
                        "file_path": file_path,
                    },
                })
                by_type[ftype] += 1
                total += 1
                print(f"[INFO]   ✓ Added to batch (total: {total}, id: {doc_id[:8]}...)")

            files_processed[ftype] += 1

    print(f"\n{'='*60}")
    print(f"[INFO] EXTRACTION SUMMARY:")
    print(f"[INFO]   Files found:     {sum(files_found.values())}")
    print(f"[INFO]   Files processed: {sum(files_processed.values())}")
    print(f"[INFO]   Documents ready: {total}")
    print(f"[INFO]   Skipped:         {skipped}")
    print(f"[INFO]   Errors:          {errors}")
    for f in formats:
        print(f"[INFO]     {f}: found={files_found.get(f,0)}, processed={files_processed.get(f,0)}, docs={by_type.get(f,0)}")
    print(f"{'='*60}")

    # 3) Bulk index with streaming_bulk for better error visibility
    if actions:
        print(f"[INFO] Starting bulk index of {len(actions)} documents...")
        
        success_count = 0
        failed_count = 0
        failed_details = []
        
        try:
            for ok, result in helpers.streaming_bulk(
                es,
                actions,
                refresh=True,
                raise_on_error=False,
                raise_on_exception=False,
                chunk_size=500,
            ):
                if ok:
                    success_count += 1
                else:
                    failed_count += 1
                    failed_details.append(result)
                    if failed_count <= 5:
                        print(f"[ERROR] Bulk item failed: {json.dumps(result, default=str)[:500]}")

            print(f"[SUCCESS] Bulk index complete: {success_count} succeeded, {failed_count} failed")

            if failed_count > 0:
                print(f"[WARN] {failed_count} documents failed to index")
                return {
                    "total": success_count,
                    "by_type": by_type,
                    "skipped": skipped,
                    "errors": errors + failed_count,
                    "warning": f"{failed_count} documents failed to index (see server logs)",
                    "failed_details": failed_details[:10]
                }
        except Exception as e:
            print(f"[ERROR] Bulk indexing failed: {e}")
            print(traceback.format_exc())
            raise HTTPException(status_code=500, detail=f"Bulk indexing failed: {str(e)}")
    else:
        print(f"[WARN] No documents to index!")

    print(f"[INFO] Indexing finished. Total indexed: {success_count}")
    print(f"{'='*60}\n")

    return {
        "total": success_count,
        "by_type": by_type,
        "skipped": skipped,
        "errors": errors
    }


# =============================================================================
# Routes - search
# =============================================================================
@app.get("/search")
def search(
    q: str = Query(..., description="Query string"),
    from_date: Optional[str] = Query(None),
    to_date:   Optional[str] = Query(None),
    file_type: Optional[str] = Query(None),
    page:      int = Query(1, ge=1),
    page_size: int = Query(5, ge=1, le=50),
):
    if es is None:
        raise HTTPException(status_code=503, detail="Elasticsearch not connected")

    filters: List[Dict[str, Any]] = []
    if file_type:
        filters.append({"term": {"file_type": file_type}})

    if from_date or to_date:
        date_range: Dict[str, Any] = {}
        if from_date:
            date_range["gte"] = from_date
        if to_date:
            date_range["lte"] = to_date
        filters.append({"range": {"mod_date": date_range}})

    main_query = {
        "bool": {
            "should": [
                {
                    "query_string": {
                        "query": q,
                        "default_field": "content",
                        "default_operator": "AND",
                        "lenient": True
                    }
                },
                {
                    "match": {
                        "content": {
                            "query": q,
                            "fuzziness": "AUTO"
                        }
                    }
                }
            ]
        }
    }

    body: Dict[str, Any] = {
        "from": (page - 1) * page_size,
        "size": page_size,
        "query": {
            "bool": {
                "must": [main_query],
                "filter": filters,
            }
        },
        "highlight": {
            "fields": {
                "content": {
                    "fragment_size": 180,
                    "number_of_fragments": 1,
                    "pre_tags": ["<mark>"],
                    "post_tags": ["</mark>"],
                }
            }
        },
    }

    try:
        resp = es.search(index=INDEX_NAME, body=body)
    except Exception as e:
        print(f"[ERROR] Search failed: {e}")
        raise HTTPException(status_code=500, detail=f"Search failed: {e}")

    total = resp["hits"]["total"]["value"]
    hits = resp["hits"]["hits"]

    results = []
    for h in hits:
        src = h.get("_source", {})
        hl = h.get("highlight", {}).get("content", [])
        snippet = hl[0] if hl else (src.get("content", "") or "")[:180]
        results.append({
            "filename":  src.get("filename"),
            "file_type": src.get("file_type"),
            "mod_date":  src.get("mod_date"),
            "score":     h.get("_score"),
            "highlight": snippet,
            "file_path": src.get("file_path"),
        })

    # Did you mean
    did_you_mean = None
    if total == 0:
        try:
            sugg_body = {
                "suggest": {
                    "text": q,
                    "simple_phrase": {
                        "term": {
                            "field": "content",
                            "suggest_mode": "always",
                        }
                    }
                }
            }
            sugg_resp = es.search(index=INDEX_NAME, body=sugg_body)
            tokens = sugg_resp.get("suggest", {}).get("simple_phrase", [])
            corrected = []
            changed = False
            for tok in tokens:
                opts = tok.get("options", [])
                if opts:
                    corrected.append(opts[0]["text"])
                    changed = True
                else:
                    corrected.append(tok.get("text", ""))
            if changed and corrected:
                did_you_mean = " ".join(corrected)
        except Exception:
            did_you_mean = None

    return {
        "total": total,
        "page": page,
        "results": results,
        "did_you_mean": did_you_mean,
    }


# =============================================================================
# Routes - stats - FIXED: Use count API for accuracy
# =============================================================================
@app.get("/stats")
def stats():
    if es is None:
        return {"total_documents": 0, "by_type": {}, "top_terms": [], "error": "Not connected"}

    try:
        if not es.indices.exists(index=INDEX_NAME):
            return {"total_documents": 0, "by_type": {}, "top_terms": []}
    except Exception:
        return {"total_documents": 0, "by_type": {}, "top_terms": [], "error": "Index check failed"}

    # ✅ Get exact count first using count API
    exact_count = None
    try:
        count_resp = es.count(index=INDEX_NAME)
        exact_count = count_resp["count"]
        print(f"[INFO] Exact document count from count API: {exact_count}")
    except Exception as e:
        print(f"[WARN] Count query failed: {e}")

    body = {
        "size": 0,
        "aggs": {
            "by_type": {
                "terms": {"field": "file_type", "size": 20}
            },
            "top_terms": {
                "terms": {
                    "field": "content.terms",
                    "size": 10
                }
            },
        }
    }

    try:
        resp = es.search(index=INDEX_NAME, body=body)
    except Exception as e:
        print(f"[ERROR] Stats query failed: {e}")
        # Fallback to keyword
        try:
            fallback_body = {
                "size": 0,
                "aggs": {
                    "by_type": {
                        "terms": {"field": "file_type", "size": 20}
                    },
                    "top_terms": {
                        "terms": {
                            "field": "content.keyword",
                            "size": 10
                        }
                    },
                }
            }
            resp = es.search(index=INDEX_NAME, body=fallback_body)
        except Exception as e2:
            return {"total_documents": 0, "by_type": {}, "top_terms": [], "error": f"Terms failed: {e}, Keyword failed: {e2}"}

    total_docs = exact_count if exact_count is not None else resp["hits"]["total"]["value"]
    by_type = {
        b["key"]: b["doc_count"]
        for b in resp["aggregations"]["by_type"]["buckets"]
    }

    top_terms_buckets = resp["aggregations"]["top_terms"]["buckets"]
    top_terms = [
        {"term": b["key"], "count": b["doc_count"]}
        for b in top_terms_buckets
    ]

    return {
        "total_documents": total_docs,
        "by_type": by_type,
        "top_terms": top_terms,
    }


# =============================================================================
# Exception handler - return JSON for all errors
# =============================================================================
@app.exception_handler(Exception)
async def generic_exception_handler(request: Request, exc: Exception):
    print(f"[ERROR] Unhandled exception: {exc}")
    print(traceback.format_exc())
    return JSONResponse(
        status_code=500,
        content={"detail": f"Internal server error: {str(exc)}"}
    )


# =============================================================================
# Frontend
# =============================================================================
HERE = os.path.dirname(os.path.abspath(__file__))
INDEX_HTML = os.path.join(HERE, "index.html")


@app.get("/")
def root():
    if os.path.exists(INDEX_HTML):
        return FileResponse(INDEX_HTML)
    return JSONResponse({"message": "index.html not found next to main.py"})