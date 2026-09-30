"""Static translation of generated pages (EN → zh / ru).

Pages are generated in English; localize() rewrites every visible text node and a few
attributes using the dictionary in i18n_strings.json. Keys are "templates":
the English text with whitespace collapsed and each number replaced by {0}, {1}, …
The same dictionary is shipped to the browser (i18n/<lang>.js) so that text produced
later by JavaScript (calculator, miner lookup, tooltips) is translated too.
"""
import html as H
import json
import os
import re

ROOT = os.path.dirname(os.path.abspath(__file__))
LANGS = {"en": ("EN", "English"), "zh": ("中文", "简体中文"), "ru": ("RU", "Русский")}
HTML_LANG = {"en": "en", "zh": "zh-CN", "ru": "ru"}

NUM = re.compile(r"(?:(?<![\w.])[-+−])?\d+(?:,\d{3})*(?:\.\d+)?")
MONTHS = ["Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"]
MON = re.compile(r"(?<=\d )(%s)\b|\b(%s)(?= \d)" % ("|".join(MONTHS), "|".join(MONTHS)))
MON_L = {"zh": [f"{i}月" for i in range(1, 13)],
         "ru": ["янв", "фев", "мар", "апр", "мая", "июн", "июл", "авг", "сен", "окт", "ноя", "дек"]}
WS = re.compile(r"\s+")
LETTER = re.compile(r"[A-Za-z]")
# blocks never translated: scripts, styles, code, and anything marked notr
SKIP = re.compile(r"(<script\b.*?</script>|<style\b.*?</style>|<pre\b.*?</pre>|<code\b.*?</code>|<!--.*?-->)", re.S | re.I)
TAG = re.compile(r"(<[^>]+>)")
ATTR = re.compile(r'\b(placeholder|title|aria-label|alt)="([^"]*)"')
META = re.compile(r'(<meta (?:name="description"|property="og:(?:title|description)") content=")([^"]*)(")')


def template(text):
    """English text → (key, (numbers, months)). Key is None if there is nothing to translate."""
    t = WS.sub(" ", text).strip()
    if not t or not LETTER.search(t):
        return None, None
    mons = [a or b for a, b in MON.findall(t)]
    t = MON.sub("\x00", t)
    nums = NUM.findall(t)
    i = iter(range(len(nums)))
    t = NUM.sub(lambda m: "{%d}" % next(i), t)
    j = iter(range(len(mons)))
    t = re.sub("\x00", lambda m: "{m%d}" % next(j), t)
    return t, (nums, mons)


_CACHE = {}


def load(lang):
    """{english template: translation} for one language, from i18n_strings.json."""
    if lang == "en":
        return {}
    if not _CACHE:
        p = os.path.join(ROOT, "i18n_strings.json")
        if os.path.exists(p):
            with open(p, encoding="utf-8") as f:
                _CACHE.update(json.load(f))
    return {k: v[lang] for k, v in _CACHE.items() if v.get(lang)}


def tr(text, dic, miss=None, lang=None):
    key, parts = template(text)
    if key is None:
        return text
    out = dic.get(key)
    if out is None and key.endswith(":") and key[:-1] in dic:
        out = dic[key[:-1]] + ("：" if lang == "zh" else ":")
    if out is None:
        if miss is not None:
            miss.add(key)
        return text
    nums, mons = parts
    ml = MON_L.get(lang) or MONTHS
    out = re.sub(r"\{(m?)(\d+)\}", lambda m: (ml[MONTHS.index(mons[int(m.group(2))])] if int(m.group(2)) < len(mons) else "")
                 if m.group(1) else (nums[int(m.group(2))] if int(m.group(2)) < len(nums) else ""), out)
    lead = text[:len(text) - len(text.lstrip())]
    trail = text[len(text.rstrip()):]
    return lead + out + trail


def _tr_text(raw, dic, miss, lang=None):
    """raw is HTML-escaped text between tags."""
    plain = H.unescape(raw)
    out = tr(plain, dic, miss, lang)
    return raw if out is plain else H.escape(out, quote=False)


def _tr_tag(tag, dic, miss, lang=None):
    tag = ATTR.sub(lambda m: f'{m.group(1)}="{H.escape(tr(H.unescape(m.group(2)), dic, miss, lang))}"', tag)
    return META.sub(lambda m: m.group(1) + H.escape(tr(H.unescape(m.group(2)), dic, miss, lang)) + m.group(3), tag)


H1 = re.compile(r"(<h1[^>]*>)(.*?)(</h1>)", re.S)


def translate_html(doc, dic, miss=None, lang=None):
    # headings with highlighted words are translated as a whole (word order differs)
    doc = H1.sub(lambda m: m.group(1) + dic.get("h1:" + m.group(2), m.group(2)) + m.group(3), doc)
    out = []
    for part in SKIP.split(doc):
        if SKIP.fullmatch(part or ""):
            out.append(part)
            continue
        for seg in TAG.split(part):
            if not seg:
                continue
            if seg.startswith("<"):
                out.append(_tr_tag(seg, dic, miss, lang))
            else:
                out.append(_tr_text(seg, dic, miss, lang))
    return "".join(out)


def collect(doc):
    miss = set()
    translate_html(doc, {}, miss)
    return miss


def localize(doc, lang, fn, domain):
    """English page → page in `lang`. fn is the file name (index.html …)."""
    dic = load(lang)
    doc = translate_html(doc, dic, None, lang)
    doc = doc.replace('<html lang="en"', f'<html lang="{HTML_LANG[lang]}"', 1)
    if domain:
        path = "" if fn == "index.html" else fn
        doc = doc.replace(f'<link rel="canonical" href="https://{domain}/{path}">',
                          f'<link rel="canonical" href="https://{domain}/{lang}/{path}">', 1)
    doc = doc.replace(f'<a class="lg on" hreflang="en"', '<a class="lg" hreflang="en"', 1)
    doc = doc.replace(f'<a class="lg" hreflang="{lang}"', f'<a class="lg on" hreflang="{lang}" aria-current="true"', 1)
    # the browser-side dictionary (translates text created later by JavaScript)
    doc = doc.replace("</body>", f'<script src="/i18n/{lang}.js" defer></script></body>', 1)
    return doc


def switcher(fn):
    path = "" if fn == "index.html" else fn
    links = "".join(
        f'<a class="lg{" on" if l == "en" else ""}" hreflang="{l}" lang="{HTML_LANG[l]}" href="/{"" if l == "en" else l + "/"}{path}" title="{full}">{short}</a>'
        for l, (short, full) in LANGS.items())
    return f'<div class="langs notr" role="group" aria-label="Language">{links}</div>'


def hreflang(fn, domain):
    if not domain:
        return ""
    path = "" if fn == "index.html" else fn
    tags = "".join(f'<link rel="alternate" hreflang="{HTML_LANG[l]}" href="https://{domain}/{"" if l == "en" else l + "/"}{path}">' for l in LANGS)
    return tags + f'<link rel="alternate" hreflang="x-default" href="https://{domain}/{path}">'


RUNTIME_JS = r"""(function(){const D=window.TSC_I18N||{};if(!D)return;
const NUM=/(?:(?<![\w.])[-+−])?\d+(?:,\d{3})*(?:\.\d+)?/g,SKIP=/^(SCRIPT|STYLE|PRE|CODE|TEXTAREA|INPUT)$/,done=new WeakMap();
const MO=['Jan','Feb','Mar','Apr','May','Jun','Jul','Aug','Sep','Oct','Nov','Dec'],ML=window.TSC_MON||MO,MONR=new RegExp('(?<=\\d )('+MO.join('|')+')\\b|\\b('+MO.join('|')+')(?= \\d)','g');
function tr(s){const t=s.replace(/\s+/g,' ').trim();if(!t||!/[A-Za-z]/.test(t))return null;const mo=[];
let u=t.replace(MONR,(m,a,b)=>{mo.push(a||b);return '\u0000'});const n=u.match(NUM)||[];let i=0,j=0;
u=u.replace(NUM,()=>'{'+(i++)+'}').replace(/\u0000/g,()=>'{m'+(j++)+'}');let v=D[u];if(v==null&&u.endsWith(':')&&D[u.slice(0,-1)]!=null)v=D[u.slice(0,-1)]+(window.TSC_COLON||':');if(v==null)return null;
const o=v.replace(/\{(m?)(\d+)\}/g,(_,m,k)=>m?(ML[MO.indexOf(mo[+k])]||''):(n[+k]!==undefined?n[+k]:''));return s.match(/^\s*/)[0]+o+s.match(/\s*$/)[0]}
function skip(el){for(;el&&el!==document.body;el=el.parentElement){if(SKIP.test(el.tagName)||el.classList&&el.classList.contains('notr'))return true}return false}
function node(n){if(done.get(n)===n.nodeValue)return;if(skip(n.parentElement))return;const o=tr(n.nodeValue);if(o!=null&&o!==n.nodeValue){n.nodeValue=o}done.set(n,n.nodeValue)}
function attrs(el){for(const a of ['placeholder','title','aria-label']){const v=el.getAttribute&&el.getAttribute(a);if(v){const o=tr(v);if(o!=null&&o!==v)el.setAttribute(a,o)}}}
function walk(root){if(root.nodeType===3){node(root);return}if(root.nodeType!==1||skip(root))return;attrs(root);
const w=document.createTreeWalker(root,NodeFilter.SHOW_TEXT|NodeFilter.SHOW_ELEMENT);let n;while(n=w.nextNode()){if(n.nodeType===3)node(n);else attrs(n)}}
walk(document.body);
new MutationObserver(ms=>{for(const m of ms){if(m.type==='characterData')node(m.target);else m.addedNodes.forEach(walk)}})
.observe(document.body,{childList:true,subtree:true,characterData:true});})();"""


def write_runtime(site):
    os.makedirs(os.path.join(site, "i18n"), exist_ok=True)
    for lang in LANGS:
        if lang == "en":
            continue
        with open(os.path.join(site, "i18n", f"{lang}.js"), "w", encoding="utf-8") as f:
            f.write("window.TSC_I18N=" + json.dumps(load(lang), ensure_ascii=False, separators=(",", ":")) + ";window.TSC_MON="
                    + json.dumps(MON_L[lang], ensure_ascii=False) + f";window.TSC_COLON='{'：' if lang == 'zh' else ':'}';\n" + RUNTIME_JS)
