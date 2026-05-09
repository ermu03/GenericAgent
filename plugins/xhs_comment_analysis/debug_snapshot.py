"""保存小红书页面数据分布调试快照。"""

import hashlib
import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
import uuid

from .browser import WORK_DIR
from .url_utils import extract_note_id_from_url

DEBUG_DIR = WORK_DIR / "debug"


DEBUG_SNAPSHOT_JS = r"""
function safeText(el) {
  return (el && el.innerText ? el.innerText : '').replace(/\s+/g, ' ').trim();
}

function truncate(value, limit = 500) {
  const text = String(value ?? '');
  return text.length > limit ? text.slice(0, limit) + `...<${text.length}>` : text;
}

function valueType(value) {
  if (value === null) return 'null';
  if (Array.isArray(value)) return 'array';
  return typeof value;
}

function primitiveSample(value) {
  const type = valueType(value);
  if (type === 'string') return truncate(value, 500);
  if (type === 'number' || type === 'boolean' || type === 'null') return value;
  if (type === 'undefined') return 'undefined';
  return undefined;
}

function shallowSample(value, depth = 0, seen = new WeakSet()) {
  const primitive = primitiveSample(value);
  if (primitive !== undefined) return primitive;
  if (!value || typeof value !== 'object') return String(value);
  if (seen.has(value)) return '<circular>';
  seen.add(value);

  if (Array.isArray(value)) {
    return {
      type: 'array',
      length: value.length,
      sample: value.slice(0, 3).map(item => shallowSample(item, depth + 1, seen))
    };
  }

  const keys = Object.keys(value);
  const out = {
    type: 'object',
    key_count: keys.length,
    keys: keys.slice(0, 80),
    sample: {}
  };
  if (depth >= 2) return out;

  for (const key of keys.slice(0, 30)) {
    try {
      out.sample[key] = shallowSample(value[key], depth + 1, seen);
    } catch (err) {
      out.sample[key] = `<error:${String(err).slice(0, 120)}>`;
    }
  }
  return out;
}

function keyFrequency(items) {
  const counts = {};
  for (const item of items.slice(0, 80)) {
    if (!item || typeof item !== 'object' || Array.isArray(item)) continue;
    for (const key of Object.keys(item)) counts[key] = (counts[key] || 0) + 1;
  }
  return Object.entries(counts)
    .sort((a, b) => b[1] - a[1] || a[0].localeCompare(b[0]))
    .slice(0, 80)
    .map(([key, count]) => ({key, count}));
}

function commentScore(item) {
  if (!item || typeof item !== 'object' || Array.isArray(item)) return 0;
  let score = 0;
  if (item.comment_id || item.commentId || item.id) score += 2;
  if (item.content || item.text || item.desc) score += 2;
  if (item.user_info || item.userInfo || item.user || item.owner) score += 2;
  if (item.sub_comments || item.subComments || item.sub_comment_list || item.subCommentList || item.replies || item.reply_list) score += 1;
  if (item.like_count || item.likeCount || item.liked_count || item.likedCount || item.interact_info || item.interactInfo) score += 1;
  return score;
}

function looksLikeNote(obj) {
  if (!obj || typeof obj !== 'object' || Array.isArray(obj)) return false;
  return Boolean(
    obj.note_id || obj.noteId ||
    obj.display_title || obj.displayTitle ||
    obj.title || obj.desc ||
    obj.note_card || obj.noteCard ||
    obj.interact_info || obj.interactInfo
  );
}

function notePreview(obj, path) {
  const interact = obj.interact_info || obj.interactInfo || {};
  const user = obj.user || obj.user_info || obj.userInfo || obj.owner || {};
  return {
    path,
    keys: Object.keys(obj).slice(0, 100),
    ids: {
      id: obj.id || '',
      note_id: obj.note_id || obj.noteId || '',
      user_id: user.user_id || user.userId || user.id || ''
    },
    text: {
      title: truncate(obj.title || obj.display_title || obj.displayTitle || '', 300),
      desc: truncate(obj.desc || obj.content || obj.note_card?.desc || obj.noteCard?.desc || '', 800)
    },
    interact_keys: Object.keys(interact || {}).slice(0, 80),
    user_keys: Object.keys(user || {}).slice(0, 80),
    sample: shallowSample(obj)
  };
}

function arrayPreview(arr, path, score) {
  return {
    path,
    length: arr.length,
    comment_score: score,
    item_key_frequency: keyFrequency(arr),
    first_item_keys: arr[0] && typeof arr[0] === 'object' ? Object.keys(arr[0]).slice(0, 120) : [],
    samples: arr.slice(0, 5).map(item => shallowSample(item))
  };
}

function walkObject(root, rootName) {
  const seen = new WeakSet();
  const noteCandidates = [];
  const commentArrays = [];

  function walk(value, path, depth) {
    if (!value || typeof value !== 'object' || depth > 8 || seen.has(value)) return;
    seen.add(value);

    if (Array.isArray(value)) {
      const score = value.slice(0, 50).reduce((total, item) => total + commentScore(item), 0);
      const pathLower = path.toLowerCase();
      if (value.length && (score >= 2 || /comment|reply|sub/.test(pathLower))) {
        commentArrays.push(arrayPreview(value, path, score));
      }
      for (let i = 0; i < Math.min(value.length, 25); i++) walk(value[i], `${path}[${i}]`, depth + 1);
      return;
    }

    if (looksLikeNote(value)) noteCandidates.push(notePreview(value, path));
    const entries = Object.entries(value).slice(0, 120);
    for (const [key, child] of entries) walk(child, `${path}.${key}`, depth + 1);
  }

  walk(root, rootName, 0);
  return {
    note_candidates: noteCandidates.slice(0, 30),
    comment_array_candidates: commentArrays
      .sort((a, b) => b.comment_score - a.comment_score || b.length - a.length)
      .slice(0, 30)
  };
}

function rootSummary(name, value) {
  return {
    name,
    type: valueType(value),
    summary: shallowSample(value)
  };
}

function collectStateRoots() {
  const fixedNames = [
    '__INITIAL_STATE__',
    '__INITIAL_SSR_STATE__',
    '__NUXT__',
    '__NEXT_DATA__',
    '__INITIAL_DATA__',
    '__XHS_STATE__'
  ];
  const names = new Set(fixedNames);
  for (const key of Object.keys(window)) {
    if (/state|store|redux|apollo|initial|ssr|note|comment/i.test(key)) names.add(key);
    if (names.size >= 80) break;
  }

  const roots = [];
  for (const name of names) {
    try {
      const value = window[name];
      if (!value || typeof value !== 'object') continue;
      roots.push({name, value});
    } catch (_) {}
  }
  return roots;
}

function collectDomDistribution() {
  const selectorGroups = {
    title: ['#detail-title', '.title', '[class*=title]'],
    content: ['#detail-desc', '.desc', '[class*=desc]', '[class*=content]'],
    author: ['.author .name', '.user-name', '[class*=author] [class*=name]', 'a[href*="/user/profile"]'],
    comments: ['[class*=comment-item]', '[class*=CommentItem]', '.comment-item', '.parent-comment', '.comment', '[class*=reply]', '[class*=sub-comment]'],
    expand_buttons: ['button', 'span', 'div', 'a']
  };
  const selectorCounts = {};
  for (const [group, selectors] of Object.entries(selectorGroups)) {
    selectorCounts[group] = selectors.map(selector => {
      try {
        return {selector, count: document.querySelectorAll(selector).length};
      } catch (err) {
        return {selector, count: -1, error: String(err).slice(0, 120)};
      }
    });
  }

  const commentNodes = [];
  const commentSelectors = selectorGroups.comments;
  for (const selector of commentSelectors) {
    try {
      for (const el of Array.from(document.querySelectorAll(selector)).slice(0, 8)) {
        const links = Array.from(el.querySelectorAll('a[href]')).slice(0, 5).map(a => a.href);
        const attrs = {};
        for (const attr of Array.from(el.attributes || []).slice(0, 20)) attrs[attr.name] = truncate(attr.value, 200);
        commentNodes.push({
          selector,
          tag: el.tagName,
          class_name: truncate(String(el.className || ''), 200),
          attrs,
          links,
          text: truncate(safeText(el), 800)
        });
      }
    } catch (_) {}
    if (commentNodes.length >= 20) break;
  }

  const scrollContainers = Array.from(document.querySelectorAll('div, main, section, body')).map(el => {
    const style = getComputedStyle(el);
    return {
      tag: el.tagName,
      id: el.id || '',
      class_name: truncate(String(el.className || ''), 180),
      overflow_y: style.overflowY,
      client_height: el.clientHeight,
      scroll_height: el.scrollHeight,
      scroll_top: el.scrollTop,
      text_preview: truncate(safeText(el), 200)
    };
  }).filter(item => item.scroll_height > item.client_height + 30)
    .sort((a, b) => b.scroll_height - a.scroll_height)
    .slice(0, 15);

  const expandTexts = Array.from(document.querySelectorAll('button, span, div, a')).map(el => safeText(el))
    .filter(text => text && text.length <= 40 && /展开|更多回复|查看|条回复|回复/.test(text))
    .slice(0, 80);

  return {selector_counts: selectorCounts, comment_nodes: commentNodes, scroll_containers: scrollContainers, expand_texts: expandTexts};
}

function collectStorageDistribution() {
  function listStorage(storage) {
    const items = [];
    for (let i = 0; i < storage.length; i++) {
      const key = storage.key(i);
      const value = storage.getItem(key) || '';
      const item = {key, length: value.length, looks_json: false, json_summary: null};
      if (/^[\[{]/.test(value.trim())) {
        try {
          const parsed = JSON.parse(value);
          item.looks_json = true;
          item.json_summary = shallowSample(parsed);
        } catch (_) {
          item.looks_json = true;
        }
      }
      items.push(item);
    }
    return items.slice(0, 120);
  }
  return {
    local_storage: listStorage(window.localStorage),
    session_storage: listStorage(window.sessionStorage)
  };
}

function stripUrl(url) {
  try {
    const u = new URL(url, location.href);
    return {
      host: u.host,
      path: u.pathname,
      query_keys: Array.from(u.searchParams.keys()).slice(0, 40),
      initiator: ''
    };
  } catch (_) {
    return {raw: truncate(url, 300)};
  }
}

function collectResources() {
  return performance.getEntriesByType('resource')
    .filter(entry => /xiaohongshu|xhscdn|xhslink|sns|api|comment|note/i.test(entry.name))
    .slice(-160)
    .map(entry => {
      const item = stripUrl(entry.name);
      item.initiator = entry.initiatorType || '';
      return item;
    });
}

const roots = collectStateRoots();
const rootSummaries = [];
let noteCandidates = [];
let commentArrayCandidates = [];
for (const root of roots) {
  rootSummaries.push(rootSummary(root.name, root.value));
  const walked = walkObject(root.value, root.name);
  noteCandidates = noteCandidates.concat(walked.note_candidates);
  commentArrayCandidates = commentArrayCandidates.concat(walked.comment_array_candidates);
}

return {
  snapshot_version: 1,
  page: {
    url: location.href,
    title: document.title || '',
    html_length: document.documentElement ? document.documentElement.outerHTML.length : 0,
    body_text_length: document.body ? document.body.innerText.length : 0,
    body_preview: truncate(document.body ? document.body.innerText : '', 1200),
    cookie_names: document.cookie ? document.cookie.split(';').map(x => x.split('=')[0].trim()).filter(Boolean).slice(0, 120) : []
  },
  state_roots: rootSummaries,
  note_candidates: noteCandidates.slice(0, 40),
  comment_array_candidates: commentArrayCandidates
    .sort((a, b) => b.comment_score - a.comment_score || b.length - a.length)
    .slice(0, 40),
  dom_distribution: collectDomDistribution(),
  storage_distribution: collectStorageDistribution(),
  resource_distribution: collectResources()
};
"""


def save_debug_snapshot(
    tab,
    *,
    input_url: str,
    phase: str,
    status_callback=None,
) -> str:
    """把当前页面的数据分布保存成 JSON 文件，供后续修正字段提取逻辑。"""
    DEBUG_DIR.mkdir(parents=True, exist_ok=True)
    path = _build_snapshot_path(input_url, phase)
    payload = {
        "phase": phase,
        "input_url": input_url,
        "captured_at": datetime.now(timezone.utc).isoformat(),
        "error": "",
        "snapshot": {},
    }
    try:
        result = tab.run_js(DEBUG_SNAPSHOT_JS, timeout=15)
        payload["snapshot"] = result if isinstance(result, dict) else {"raw_result": result}
    except Exception as exc:
        payload["error"] = f"{type(exc).__name__}: {exc}"

    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    _emit(
        status_callback,
        "debug_snapshot_saved",
        message=f"已保存小红书页面调试快照: {path}",
        debug_snapshot_path=str(path),
    )
    return str(path)


def _build_snapshot_path(input_url: str, phase: str) -> Path:
    """生成稳定可读的调试快照文件名。"""
    note_id = extract_note_id_from_url(input_url)
    if note_id:
        record_hint = note_id[:40]
    else:
        record_hint = hashlib.sha256(input_url.encode("utf-8")).hexdigest()[:8]
    safe_phase = "".join(ch if ch.isalnum() or ch in {"_", "-"} else "_" for ch in phase)[:40]
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    return DEBUG_DIR / f"xhs_debug_{record_hint}_{safe_phase}_{stamp}_{uuid.uuid4().hex[:8]}.json"


def _emit(callback, event: str, **payload: Any) -> None:
    """调试快照事件只作通知，不能影响主采集流程。"""
    if not callback:
        return
    try:
        callback(event, payload)
    except Exception:
        pass
