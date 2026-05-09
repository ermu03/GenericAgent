"""从已经打开的小红书页面中提取帖子和评论数据。"""

import re
import time
from typing import Any


STATE_AND_DOM_JS = r"""
function safeText(el) {
  return (el && el.innerText ? el.innerText : '').replace(/\s+/g, ' ').trim();
}

function attr(el, name) {
  return el ? (el.getAttribute(name) || '') : '';
}

function numberText(text) {
  if (!text) return 0;
  text = String(text).trim();
  const m = text.match(/([0-9]+(?:\.[0-9]+)?)(万|w|W|k|K)?/);
  if (!m) return 0;
  let n = parseFloat(m[1]);
  const unit = m[2] || '';
  if (unit === '万' || unit === 'w' || unit === 'W') n *= 10000;
  if (unit === 'k' || unit === 'K') n *= 1000;
  return Math.round(n);
}

function uniq(values) {
  return Array.from(new Set((values || []).filter(Boolean)));
}

function pickInitialState() {
  const candidates = [
    window.__INITIAL_STATE__,
    window.__INITIAL_SSR_STATE__,
    window.__NUXT__,
    window.__NEXT_DATA__
  ];
  for (const item of candidates) {
    if (item && typeof item === 'object') return item;
  }
  return null;
}

function findNoteObject(obj, noteId, depth = 0, seen = new Set()) {
  if (!obj || typeof obj !== 'object' || depth > 8 || seen.has(obj)) return null;
  seen.add(obj);
  if (obj.note && typeof obj.note === 'object') {
    const note = obj.note;
    const id = note.note_id || note.noteId || note.id;
    if (!noteId || id === noteId || note.title || note.desc || note.interact_info) return note;
  }
  const id = obj.note_id || obj.noteId || obj.id;
  if ((obj.title || obj.desc || obj.interact_info || obj.user) && (!noteId || id === noteId)) return obj;
  for (const value of Object.values(obj)) {
    const found = findNoteObject(value, noteId, depth + 1, seen);
    if (found) return found;
  }
  return null;
}

function findCommentArrays(obj, depth = 0, seen = new Set(), found = []) {
  if (!obj || typeof obj !== 'object' || depth > 9 || seen.has(obj)) return found;
  seen.add(obj);
  if (Array.isArray(obj)) {
    const score = obj.filter(item => item && typeof item === 'object' && (
      item.comment_id || item.commentId ||
      ((item.content || item.text) && (item.user_info || item.userInfo || item.user || item.owner))
    )).length;
    if (score >= 1) found.push(obj);
    for (const item of obj.slice(0, 30)) findCommentArrays(item, depth + 1, seen, found);
    return found;
  }
  for (const value of Object.values(obj)) {
    findCommentArrays(value, depth + 1, seen, found);
  }
  return found;
}

function noteIdFromUrl() {
  const parts = location.pathname.split('/').filter(Boolean);
  const names = new Set(['explore', 'search_result', 'discovery', 'item']);
  if (parts.length >= 2 && names.has(parts[parts.length - 2])) {
    const last = parts[parts.length - 1] || '';
    return /^[0-9a-zA-Z_-]+$/.test(last) ? last : '';
  }
  const params = new URLSearchParams(location.search);
  const redirectPath = params.get('redirectPath') || '';
  if (redirectPath) {
    const redirectParts = redirectPath.split('?')[0].split('/').filter(Boolean);
    if (redirectParts.length >= 2 && names.has(redirectParts[redirectParts.length - 2])) {
      const last = redirectParts[redirectParts.length - 1] || '';
      return /^[0-9a-zA-Z_-]+$/.test(last) ? last : '';
    }
  }
  return '';
}

function normalizeComment(raw, parentId = null, level = 1) {
  if (!raw || typeof raw !== 'object') return null;
  const user = raw.user_info || raw.userInfo || raw.user || raw.owner || {};
  const interact = raw.interact_info || raw.interactInfo || {};
  const id = raw.comment_id || raw.commentId || raw.id || '';
  const parent = raw.parent_comment_id || raw.parentCommentId || raw.parent_id || raw.parentId || parentId;
  const text = raw.content || raw.text || raw.desc || raw.note || '';
  if (!text && !id) return null;
  const userId = user.user_id || user.userId || user.id || raw.user_id || raw.userId || '';
  const profileUrl = userId ? `https://www.xiaohongshu.com/user/profile/${userId}` : '';
  return {
    comment_id: id,
    parent_comment_id: parent || null,
    level,
    owner_user_id: userId,
    owner_nickname: user.nickname || user.nick_name || user.name || raw.nickname || '',
    owner_profile_url: profileUrl,
    text: text,
    liked_count: numberText(raw.like_count || raw.likeCount || raw.liked_count || raw.likedCount || interact.liked_count || interact.like_count),
    reply_count: numberText(raw.sub_comment_count || raw.subCommentCount || raw.reply_count || raw.replyCount),
    created_at: raw.create_time || raw.createTime || raw.time || raw.created_at || '',
    metadata: {
      source: 'state',
      ip_location: raw.ip_location || raw.ipLocation || '',
      raw_keys: Object.keys(raw).slice(0, 60),
      key: ''
    }
  };
}

function normalizeCommentTree(items) {
  const out = [];
  for (const raw of items || []) {
    if (!raw || typeof raw !== 'object') continue;
    const parent = normalizeComment(raw, null, 1);
    if (parent) out.push(parent);
    const children = raw.sub_comments || raw.subComments || raw.sub_comment_list || raw.subCommentList || raw.replies || raw.reply_list || [];
    if (Array.isArray(children)) {
      const parentId = parent ? parent.comment_id : (raw.comment_id || raw.commentId || raw.id || null);
      for (const child of children) {
        const normalizedChild = normalizeComment(child, parentId, 2);
        if (normalizedChild) out.push(normalizedChild);
      }
    }
  }
  return out.map(c => {
    c.metadata.key = commentKey(c);
    return c;
  });
}

function extractCommentsFromState(state) {
  const arrays = findCommentArrays(state);
  let best = [];
  for (const arr of arrays) {
    const comments = normalizeCommentTree(arr);
    if (comments.length > best.length) best = comments;
  }
  return best;
}

function normalizeNote(raw) {
  if (!raw || typeof raw !== 'object') return {};
  const interact = raw.interact_info || raw.interactInfo || {};
  const user = raw.user || raw.user_info || raw.userInfo || {};
  const tags = raw.tag_list || raw.tagList || raw.hash_tag || raw.hashTags || [];
  return {
    post: {
      post_id: raw.note_id || raw.noteId || raw.id || noteIdFromUrl(),
      title: raw.title || raw.display_title || raw.displayTitle || '',
      content: raw.desc || raw.content || raw.note_card?.desc || '',
      tags: Array.isArray(tags) ? tags.map(t => t.name || t.title || t.tag_name || '').filter(Boolean) : [],
      created_at: raw.time || raw.create_time || raw.createTime || raw.last_update_time || '',
      liked_count: numberText(interact.liked_count || interact.likedCount || interact.like_count || interact.likeCount),
      collected_count: numberText(interact.collected_count || interact.collectedCount || interact.collect_count || interact.collectCount),
      comment_count: numberText(interact.comment_count || interact.commentCount),
      shared_count: numberText(interact.share_count || interact.shareCount),
      metadata: { raw_keys: Object.keys(raw).slice(0, 80) }
    },
    post_owner: {
      user_id: user.user_id || user.userId || user.id || '',
      nickname: user.nickname || user.nick_name || user.name || '',
      profile_url: user.user_id || user.userId || user.id ? `https://www.xiaohongshu.com/user/profile/${user.user_id || user.userId || user.id}` : '',
      bio: user.desc || user.description || '',
      location: user.ip_location || user.location || '',
      verified: user.red_official_verified || user.verified || null,
      metadata: { avatar: user.avatar || user.image || '' }
    }
  };
}

function extractDomPost() {
  const titleEl = document.querySelector('#detail-title, .title, [class*=title]');
  const descEl = document.querySelector('#detail-desc, .desc, [class*=desc]');
  const authorEl = document.querySelector('.author .name, .user-name, [class*=author] [class*=name]');
  const bodyText = safeText(document.body);
  return {
    post: {
      post_id: noteIdFromUrl(),
      title: safeText(titleEl),
      content: safeText(descEl),
      tags: uniq(Array.from(document.querySelectorAll('a, span')).map(e => safeText(e)).filter(t => t.startsWith('#')).map(t => t.replace(/^#/, ''))),
      metadata: { body_preview: bodyText.slice(0, 500) }
    },
    post_owner: {
      nickname: safeText(authorEl),
      metadata: {}
    }
  };
}

function commentKey(c) {
  return [c.comment_id, c.owner_user_id, c.owner_nickname, c.text, c.created_at, c.parent_comment_id].join('|');
}

function looksLikeComment(el) {
  const cls = String(el.className || '').toLowerCase();
  if (cls.includes('comment')) return true;
  const txt = safeText(el);
  return txt.length >= 2 && txt.length < 1000 && /赞|回复|小时前|分钟前|昨天|IP属地|展开/.test(txt);
}

function extractCommentsFromDom() {
  const selectors = [
    '[class*=comment-item]',
    '[class*=CommentItem]',
    '.comment-item',
    '.parent-comment',
    '.comment'
  ];
  let nodes = [];
  for (const selector of selectors) {
    nodes = Array.from(document.querySelectorAll(selector)).filter(looksLikeComment);
    if (nodes.length) break;
  }
  const comments = [];
  for (const el of nodes) {
    const textEl = el.querySelector('[class*=content], [class*=text], .content, .text') || el;
    const authorEl = el.querySelector('a[href*="/user/profile"], [class*=author], [class*=name]');
    const profile = authorEl && authorEl.closest('a') ? authorEl.closest('a') : el.querySelector('a[href*="/user/profile"]');
    const profileUrl = profile ? new URL(attr(profile, 'href'), location.href).href : '';
    const ownerId = (profileUrl.match(/\/user\/profile\/([^/?#]+)/) || [])[1] || '';
    const rawText = safeText(textEl);
    if (!rawText) continue;
    const isChild = Boolean(el.closest('[class*=reply], [class*=sub]')) || /reply|sub/.test(String(el.className || '').toLowerCase());
    comments.push({
      comment_id: attr(el, 'data-id') || attr(el, 'data-comment-id') || '',
      parent_comment_id: isChild ? 'unknown_parent' : null,
      level: isChild ? 2 : 1,
      owner_user_id: ownerId,
      owner_nickname: safeText(authorEl),
      owner_profile_url: profileUrl,
      text: rawText,
      liked_count: numberText(safeText(el.querySelector('[class*=like], [class*=count]'))),
      reply_count: 0,
      created_at: '',
      metadata: {
        dom_class: String(el.className || '').slice(0, 200),
        key: ''
      }
    });
  }
  return comments.map(c => {
    c.metadata.key = commentKey(c);
    return c;
  });
}

function detectPageState() {
  const text = document.body ? document.body.innerText : '';
  const title = document.title || '';
  const lowerUrl = location.href.toLowerCase();
  const combined = text + title + lowerUrl;
  const hasLogin = /登录|扫码|验证码|安全验证|拖动滑块|验证/.test(text + title);
  if (/404|not.?found/i.test(title + text)) return 'not_found';
  if (/ip at risk|error_code=300012|安全限制/i.test(combined)) return 'risk_control';
  if (/验证码|安全验证|拖动滑块|verify|captcha/i.test(combined)) return 'verification_required';
  if (/登录|扫码登录|请先登录/.test(text + title)) return 'login_required';
  if (hasLogin) return 'login_or_verification';
  return 'normal';
}

const noteId = noteIdFromUrl();
const state = pickInitialState();
const note = normalizeNote(findNoteObject(state, noteId));
const dom = extractDomPost();
const comments = extractCommentsFromState(state).concat(extractCommentsFromDom());
return {
  page_state: detectPageState(),
  state_found: Boolean(state),
  note: {
    post: Object.assign({}, dom.post, note.post || {}),
    post_owner: Object.assign({}, dom.post_owner, note.post_owner || {})
  },
  comments,
  html_length: document.documentElement.outerHTML.length,
  body_text_length: document.body ? document.body.innerText.length : 0
};
"""


EXPAND_REPLIES_JS = r"""
const keywords = ['展开', '更多回复', '查看', '条回复'];
let clicked = 0;
for (const el of Array.from(document.querySelectorAll('button, span, div, a'))) {
  const text = (el.innerText || '').trim();
  if (!text || text.length > 30) continue;
  if (!keywords.some(k => text.includes(k))) continue;
  const rect = el.getBoundingClientRect();
  if (rect.width <= 0 || rect.height <= 0) continue;
  try {
    el.click();
    clicked++;
    if (clicked >= 8) break;
  } catch (_) {}
}
return clicked;
"""


SCROLL_JS = r"""
const containers = Array.from(document.querySelectorAll('div, main, section')).filter(el => {
  const style = getComputedStyle(el);
  return /(auto|scroll)/.test(style.overflowY) && el.scrollHeight > el.clientHeight + 50;
});
const target = containers.sort((a, b) => b.scrollHeight - a.scrollHeight)[0] || document.scrollingElement || document.documentElement;
const before = target.scrollTop;
target.scrollTop = Math.min(target.scrollTop + 900, target.scrollHeight);
return {
  before,
  after: target.scrollTop,
  scrollHeight: target.scrollHeight,
  clientHeight: target.clientHeight
};
"""


def _clean_text(value: Any) -> str:
    """清理页面文本，避免空白和换行影响去重。"""
    return re.sub(r"\s+", " ", str(value or "")).strip()


def _comment_key(comment: dict[str, Any]) -> str:
    """生成评论去重键。

    小红书页面不一定稳定暴露 comment_id，因此要用文本、作者和父评论
    关系兜底。
    """
    parts = [
        comment.get("comment_id"),
        comment.get("owner_user_id"),
        comment.get("owner_nickname"),
        comment.get("text"),
        comment.get("created_at"),
        comment.get("parent_comment_id"),
    ]
    return "|".join(_clean_text(p) for p in parts)


def _merge_comments(existing: dict[str, dict[str, Any]], comments: list[dict[str, Any]], limit: int) -> None:
    """把本轮页面可见评论合并到总表。"""
    for comment in comments:
        text = _clean_text(comment.get("text"))
        if not text:
            continue
        comment["text"] = text
        key = _comment_key(comment)
        comment.setdefault("metadata", {})
        comment["metadata"]["dedupe_key"] = key
        if key not in existing:
            existing[key] = comment
        if len(existing) >= limit:
            break


def _build_comment_owners(comments: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """从评论列表中汇总评论作者基础信息。"""
    owners: dict[str, dict[str, Any]] = {}
    for comment in comments:
        owner_id = _clean_text(comment.get("owner_user_id"))
        nickname = _clean_text(comment.get("owner_nickname"))
        profile_url = _clean_text(comment.get("owner_profile_url"))
        key = owner_id or profile_url or nickname
        if not key or key in owners:
            continue
        owners[key] = {
            "user_id": owner_id,
            "nickname": nickname,
            "profile_url": profile_url,
            "bio": "",
            "location": "",
            "followers_count": None,
            "following_count": None,
            "liked_count": None,
            "verified": None,
            "profile_tags": [],
            "metadata": {},
        }
    return list(owners.values())


def _quality_status(page_state: str, comment_count: int) -> str:
    """根据页面状态和评论数判断采集状态。"""
    if page_state in {"login_required", "verification_required", "login_or_verification", "risk_control", "not_found"}:
        return "failed"
    if page_state != "normal" or comment_count == 0:
        return "partial_success"
    return "success"


def extract_note_data(tab: Any, *, comment_limit: int = 600) -> dict[str, Any]:
    """从 DrissionPage tab 中提取原始帖子数据。

    实现原则：
    - 优先读取页面已加载的状态对象，减少对 DOM 结构的依赖。
    - 同时使用 DOM 启发式兜底，因为不同小红书页面入口结构会有差异。
    - 评论采用“滚动一段、采集一轮、合并去重”的方式，避免虚拟列表丢数据。
    """
    started_at = time.time()
    warnings: list[str] = []
    errors: list[str] = []
    comments_by_key: dict[str, dict[str, Any]] = {}
    post: dict[str, Any] = {}
    post_owner: dict[str, Any] = {}
    page_state = "unknown"
    has_more_comments = None

    try:
        tab.wait.doc_loaded(timeout=20)
    except Exception as exc:
        warnings.append(f"页面加载等待超时: {exc}")

    # 多轮滚动采集。连续几轮没有新增评论后停止，避免无意义地滚动。
    no_growth_rounds = 0
    for round_no in range(30):
        try:
            result = tab.run_js(STATE_AND_DOM_JS, timeout=12)
        except Exception as exc:
            errors.append(f"页面数据提取失败: {exc}")
            break

        if isinstance(result, dict):
            page_state = result.get("page_state") or page_state
            note = result.get("note") or {}
            post.update(note.get("post") or {})
            post_owner.update(note.get("post_owner") or {})
            before_count = len(comments_by_key)
            _merge_comments(comments_by_key, result.get("comments") or [], comment_limit)
            if len(comments_by_key) == before_count:
                no_growth_rounds += 1
            else:
                no_growth_rounds = 0
        else:
            warnings.append("页面提取脚本没有返回字典结果")

        if page_state in {"login_required", "verification_required", "login_or_verification", "risk_control", "not_found"}:
            break
        if len(comments_by_key) >= comment_limit:
            has_more_comments = True
            break
        if no_growth_rounds >= 5 and round_no >= 5:
            has_more_comments = False
            break

        # 展开二级评论后再滚动。点击失败不影响主流程。
        try:
            clicked = tab.run_js(EXPAND_REPLIES_JS, timeout=5)
            if clicked:
                time.sleep(0.8)
        except Exception:
            pass

        try:
            scroll_result = tab.run_js(SCROLL_JS, timeout=5)
            if isinstance(scroll_result, dict) and scroll_result.get("after") == scroll_result.get("before"):
                no_growth_rounds += 1
            time.sleep(1.0)
        except Exception as exc:
            warnings.append(f"评论区滚动失败: {exc}")
            break

    comments = list(comments_by_key.values())[:comment_limit]
    comment_owners = _build_comment_owners(comments)
    level1_count = sum(1 for comment in comments if int(comment.get("level") or 1) == 1)
    level2_count = sum(1 for comment in comments if int(comment.get("level") or 1) == 2)
    missing_fields = []
    for field in ("post_id", "title", "content"):
        if not _clean_text(post.get(field)):
            missing_fields.append(f"post.{field}")
    if not _clean_text(post_owner.get("nickname")):
        missing_fields.append("post_owner.nickname")

    status = _quality_status(page_state, len(comments))
    if page_state == "risk_control":
        warnings.append("页面触发小红书安全限制，通常需要更换网络、登录态或人工验证后重试")
    elif page_state == "login_or_verification":
        warnings.append("页面需要登录或验证，已保存截图供用户处理")
    elif not comments:
        warnings.append("未采集到评论，可能是页面未登录、评论区未加载或 DOM 结构变化")

    return {
        "post": post,
        "post_owner": post_owner,
        "comments": comments,
        "comment_owners": comment_owners,
        "quality": {
            "status": status,
            "comment_count_collected": len(comments),
            "level1_comment_count_collected": level1_count,
            "level2_comment_count_collected": level2_count,
            "comment_limit": comment_limit,
            "comment_owner_count_collected": len(comment_owners),
            "has_more_comments": has_more_comments,
            "missing_fields": missing_fields,
            "warnings": warnings,
            "errors": errors,
            "login_required": page_state == "login_required",
            "verification_required": page_state in {"verification_required", "login_or_verification", "risk_control"},
            "page_state": page_state,
            "elapsed_seconds": round(time.time() - started_at, 2),
        },
    }
