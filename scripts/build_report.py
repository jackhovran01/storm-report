#!/usr/bin/env python3
"""Build a standalone HTML industry research report from Markdown.

Usage:
    python build_report.py report.md -o report.html [--title "自定义标题"] [--lint] [--strict]

Input Markdown conventions (storm-report 写作规范):
    # 标题               -> 报告标题（第一个 # 作为标题，不进入目录）
    ## / ### / ####      -> 二至四级标题（二、三级进入目录）
    [1] / [2,3] / [4-6]  -> 正文引用角标，渲染为可点击上标（GB/T 7714 顺序编码制）
                            仅 1-3 位数字视为引用，[2024] 这类年份不会被误判
    ## 参考文献          -> 之后每个非空行按一条 GB/T 7714 文献渲染，行首 [n] 作为锚点
                            行尾 [A级] / [B级] 渲染为可信级别徽标；`>` 开头的行渲染为表下注释
    ```chart            -> 图表代码块，支持 bar / line / pie：
        type: bar
        title: 市场规模（亿美元）    (可选)
        data: 2021,75;2022,92;2023,105   (label,value 逗号分隔；条目用分号)
    ```
    ```任意语言          -> 普通代码块，渲染为 <pre><code>
    | a | b |            -> 表格（连续多行，首行为表头，次行支持 :---: 右/居中对齐）
    - 项目 / 1. 项目      -> 无序 / 有序列表（有序列表保留起始编号）
    > 引文               -> 块引用（参考文献区内则作为表下注释）
    **加粗** *斜体* `代码` -> 行内格式

校验（--lint）：检查引用断号、悬空引用、未引用文献、EB/OL 缺 URL 或缺引用日期、
缺可信级别徽标、低质信源域名。发现 ERROR 时以退出码 1 结束（--strict 时构建后同样生效）。

Output: 单文件 HTML，内嵌 CSS 与 SVG 图表，无外部依赖，可离线打开。
"""

import argparse
import html
import math
import os
import re
import sys

# ---------- SVG 图表 ----------

PIE_COLORS = ["#378ADD", "#1D9E75", "#EF9F27", "#D85A30",
              "#7F77DD", "#97C459", "#ED93B1", "#5F5E5A"]
BAR_COLOR = "#378ADD"
LINE_COLOR = "#1D9E75"


def esc(t):
    return html.escape(str(t), quote=False)


def _fmt(v):
    """坐标轴数值格式化：避免 105.0000001 这类长尾。"""
    a = abs(v)
    if a >= 100:
        return f"{v:.0f}"
    if a >= 10:
        return f"{v:.1f}".rstrip("0").rstrip(".")
    if a == 0:
        return "0"
    return f"{v:g}"


def _parse_chart_data(data_str):
    pairs = []
    for item in data_str.split(";"):
        item = item.strip()
        if not item:
            continue
        if "," in item:
            label, val = item.rsplit(",", 1)
        else:
            label, val = "", item
        try:
            pairs.append((label.strip(), float(val.strip())))
        except ValueError:
            continue
    return pairs


def _svg_wrap(title, inner, w=680, h=320):
    t = (f'<text x="{w/2}" y="22" text-anchor="middle" font-size="14" font-weight="500" '
         f'fill="#2C2C2A">{esc(title)}</text>') if title else ""
    return (f'<svg viewBox="0 0 {w} {h}" width="100%" xmlns="http://www.w3.org/2000/svg" role="img">'
            f'<title>{esc(title or "图表")}</title>{t}{inner}</svg>')


def _y_axis(parts, pad_l, pad_t, plot_h, pad_r, maxv, w):
    """画网格线 + Y 轴刻度值（0 / 25% / 50% / 75% / 100% of maxv）。"""
    for gv in range(0, 5):
        gy = pad_t + plot_h - (gv / 4) * plot_h
        parts.append(f'<line x1="{pad_l}" y1="{gy:.1f}" x2="{w-pad_r}" y2="{gy:.1f}" '
                     f'stroke="#E5E3DC" stroke-width="0.5"/>')
        parts.append(f'<text x="{pad_l-8}" y="{gy:.1f}" text-anchor="end" dominant-baseline="central" '
                     f'font-size="11" fill="#888780">{_fmt((gv/4)*maxv)}</text>')


def svg_bar(title, data_str):
    pairs = _parse_chart_data(data_str)
    if not pairs:
        return ""
    W, H, pad_l, pad_r, pad_t, pad_b = 680, 300, 64, 16, 48, 44
    plot_w, plot_h = W - pad_l - pad_r, H - pad_t - pad_b
    maxv = max(v for _, v in pairs) or 1.0
    n = len(pairs)
    gap = plot_w / (n + 1)
    bar_w = gap * 0.55
    dense = n > 8
    parts = []
    _y_axis(parts, pad_l, pad_t, plot_h, pad_r, maxv, W)
    for i, (label, v) in enumerate(pairs):
        bh = max((v / maxv) * plot_h, 1.5)
        x = pad_l + gap * i + (gap - bar_w) / 2
        y = pad_t + plot_h - bh
        parts.append(f'<rect x="{x:.1f}" y="{y:.1f}" width="{bar_w:.1f}" height="{bh:.1f}" '
                     f'rx="2" fill="{BAR_COLOR}"/>')
        parts.append(f'<text x="{x+bar_w/2:.1f}" y="{y-6:.1f}" text-anchor="middle" font-size="11" '
                     f'fill="#444441">{v:g}</text>')
        if dense and i % 2 == 1:
            continue
        fs = 9 if dense else 11
        parts.append(f'<text x="{x+bar_w/2:.1f}" y="{pad_t+plot_h+16:.1f}" text-anchor="middle" '
                     f'font-size="{fs}" fill="#5F5E5A">{esc(label)}</text>')
    return _svg_wrap(title, "".join(parts), w=W, h=H)


def svg_line(title, data_str):
    pairs = _parse_chart_data(data_str)
    if not pairs:
        return ""
    W, H, pad_l, pad_r, pad_t, pad_b = 680, 300, 64, 16, 48, 44
    plot_w, plot_h = W - pad_l - pad_r, H - pad_t - pad_b
    maxv = max(v for _, v in pairs) or 1.0
    n = len(pairs)
    pts = []
    for i, (label, v) in enumerate(pairs):
        x = pad_l + (i / (n - 1) if n > 1 else 0) * plot_w
        y = pad_t + plot_h - (v / maxv) * plot_h
        pts.append((x, y, label, v))
    parts = []
    _y_axis(parts, pad_l, pad_t, plot_h, pad_r, maxv, W)
    poly = " ".join(f"{x:.1f},{y:.1f}" for x, y, _, _ in pts)
    parts.append(f'<polyline points="{poly}" fill="none" stroke="{LINE_COLOR}" stroke-width="2"/>')
    dense = n > 8
    for i, (x, y, label, v) in enumerate(pts):
        parts.append(f'<circle cx="{x:.1f}" cy="{y:.1f}" r="3.5" fill="{LINE_COLOR}"/>')
        parts.append(f'<text x="{x:.1f}" y="{y-8:.1f}" text-anchor="middle" font-size="11" '
                     f'fill="#444441">{v:g}</text>')
        if dense and i % 2 == 1:
            continue
        fs = 9 if dense else 11
        parts.append(f'<text x="{x:.1f}" y="{pad_t+plot_h+16:.1f}" text-anchor="middle" '
                     f'font-size="{fs}" fill="#5F5E5A">{esc(label)}</text>')
    return _svg_wrap(title, "".join(parts), w=W, h=H)


def svg_pie(title, data_str):
    """饼图：左半区画扇形，右半区用纯 SVG 图例（不用 HTML <table>，SVG 内无法直接嵌入 HTML）。"""
    pairs = _parse_chart_data(data_str)
    if not pairs:
        return ""
    total = sum(v for _, v in pairs) or 1.0
    cx, cy, r = 168, 165, 100
    start = 90  # 从 12 点方向顺时针
    parts = []
    if len(pairs) == 1:
        parts.append(f'<circle cx="{cx}" cy="{cy}" r="{r}" fill="{PIE_COLORS[0]}"/>')
    for i, (label, v) in enumerate(pairs):
        if len(pairs) == 1:
            break
        sweep = (v / total) * 360
        a0 = math.radians(start)
        a1 = math.radians(start + sweep)
        x0, y0 = cx + r * math.cos(a0), cy - r * math.sin(a0)
        x1, y1 = cx + r * math.cos(a1), cy - r * math.sin(a1)
        large = 1 if sweep > 180 else 0
        color = PIE_COLORS[i % len(PIE_COLORS)]
        parts.append(f'<path d="M{cx} {cy} L{x0:.1f} {y0:.1f} A{r} {r} 0 {large} 1 {x1:.1f} {y1:.1f} Z" '
                     f'fill="{color}"/>')
        start += sweep
    ly = cy - r + 6
    step = 22
    for i, (label, v) in enumerate(pairs):
        color = PIE_COLORS[i % len(PIE_COLORS)]
        pct = v / total * 100
        shown = label if len(label) <= 16 else label[:15] + "…"
        parts.append(f'<rect x="380" y="{ly-9}" width="10" height="10" rx="2" fill="{color}"/>')
        parts.append(f'<text x="398" y="{ly}" dominant-baseline="central" font-size="12" '
                     f'fill="#2C2C2A">{esc(shown)}</text>')
        parts.append(f'<text x="648" y="{ly}" text-anchor="end" dominant-baseline="central" '
                     f'font-size="12" fill="#5F5E5A">{v:g} ({pct:.1f}%)</text>')
        ly += step
    return _svg_wrap(title, "".join(parts), w=680, h=330)


CHART_RENDERERS = {"bar": svg_bar, "line": svg_line, "pie": svg_pie}


def render_chart_block(block):
    props = {}
    for line in block.strip().splitlines():
        line = line.strip()
        if ":" in line:
            k, v = line.split(":", 1)
            props[k.strip().lower()] = v.strip()
    ctype = props.get("type", "bar")
    renderer = CHART_RENDERERS.get(ctype, svg_bar)
    return renderer(props.get("title", ""), props.get("data", ""))


# ---------- Markdown -> HTML ----------

# 仅 1-3 位数字视为引用角标，避免 [2024] 这类年份被误判
CITE_RE = re.compile(r"\[(\d{1,3}(?:[,\-]\d{1,3})*)\]")
HEADING_RE = re.compile(r"^(#{1,6})\s+(.+?)\s*$")
FENCE_RE = re.compile(r"^\s*```\s*([\w+-]*)\s*$")
SEP_CELL_RE = re.compile(r"^:?-{1,}:?$")


def inline(text):
    text = esc(text)
    text = re.sub(r"`([^`]+)`", r"<code>\1</code>", text)
    text = re.sub(r"\*\*(.+?)\*\*", r"<strong>\1</strong>", text)
    text = re.sub(r"(?<!\*)\*(?!\*)(.+?)(?<!\*)\*(?!\*)", r"<em>\1</em>", text)

    def cite_repl(m):
        links = []
        for p in m.group(1).split(","):
            if "-" in p:
                a, b = p.split("-", 1)
                links.append(f'<a href="#ref-{a}">{a}&ndash;{b}</a>')
            else:
                links.append(f'<a href="#ref-{p}">{p}</a>')
        return '<sup class="cite">[' + ",".join(links) + "]</sup>"

    return CITE_RE.sub(cite_repl, text)


def _align_of(cell):
    if cell.startswith(":") and cell.endswith(":"):
        return "center"
    if cell.endswith(":"):
        return "right"
    return "left"


def convert(md_text):
    lines = md_text.splitlines()
    body = []
    toc = []
    anchors = {"sec": 0}
    title = ""
    fences = None          # None | ("chart", buf) | ("code", buf, lang)
    in_refs = False
    refs = []
    ref_note = []
    table_buf = []
    table_align = []
    list_kind = None
    list_start = 1
    bq_buf = []

    def flush_table():
        nonlocal table_buf, table_align
        if not table_buf:
            table_align = []
            return
        rows = [[inline(c.strip()) for c in r.strip().strip("|").split("|")] for r in table_buf]
        align = table_align
        table_buf, table_align = [], []
        if not rows:
            return
        hdr = rows[0]
        th = "".join(f'<th style="text-align:{align[i] if i < len(align) else "left"}">{c}</th>'
                     for i, c in enumerate(hdr))
        trs = "".join(
            "<tr>" + "".join(f'<td style="text-align:{align[i] if i < len(align) else "left"}">{c}</td>'
                             for i, c in enumerate(r)) + "</tr>"
            for r in rows[1:])
        body.append(f'<div class="tbl-wrap"><table><thead><tr>{th}</tr></thead>'
                    f'<tbody>{trs}</tbody></table></div>')

    def flush_list():
        nonlocal list_kind, list_start
        if list_kind is None:
            return
        body.append("</ol>" if list_kind == "ol" else "</ul>")
        list_kind, list_start = None, 1

    def close_list(kind, start=1):
        nonlocal list_kind, list_start
        if list_kind is not None and (list_kind != kind or (kind == "ol" and list_start != start)):
            flush_list()
        if list_kind is None:
            body.append(f'<ol start="{start}">' if kind == "ol" else "<ul>")
            list_kind, list_start = kind, start

    def flush_bq():
        nonlocal bq_buf
        if not bq_buf:
            return
        inner = "".join(f"<p>{inline(t)}</p>" for t in bq_buf)
        body.append(f'<blockquote class="quote">{inner}</blockquote>')
        bq_buf = []

    for line_raw in lines:
        line = line_raw.rstrip()

        # ---- 围栏块（图表 / 代码）----
        if fences is None:
            mf = FENCE_RE.match(line)
            if mf:
                lang = mf.group(1).lower()
                if lang == "chart":
                    fences = ["chart", []]
                else:
                    fences = ["code", [], lang]
                continue
        else:
            if FENCE_RE.match(line):
                kind, buf, lang = fences[0], fences[1], (fences[2] if len(fences) > 2 else "")
                fences = None
                flush_table()
                flush_list()
                flush_bq()
                if kind == "chart":
                    body.append('<figure class="chart">' + render_chart_block("\n".join(buf)) + "</figure>")
                else:
                    cls = f' class="language-{esc(lang)}"' if lang else ""
                    body.append(f'<pre class="code"><code{cls}>{esc(chr(10).join(buf))}</code></pre>')
                continue
            fences[1].append(line_raw)
            continue

        # ---- 参考文献区 ----
        if not in_refs and re.match(r"^\s*#{1,3}\s*(参考文献|References)\s*$", line):
            in_refs = True
            flush_table()
            flush_list()
            flush_bq()
            continue
        # 参考文献区遇到下一个标题（如「术语表」「附录」「致谢」）即结束，
        # 否则其后的所有条目都会被误判为文献，产生大量「未被正文引用」假告警。
        if in_refs and HEADING_RE.match(line):
            in_refs = False
        if in_refs:
            s = line.strip()
            if s.startswith(">"):
                ref_note.append(s.lstrip(">").strip())
            elif s:
                m = re.match(r"^\s*\[\s*(\d+)\s*\]\s*(.*)$", s)
                txt = m.group(2) if m else s
                n = m.group(1) if m else str(len(refs) + 1)
                badge = ""
                mb = re.search(r"\[\s*([AB])\s*级\s*\]\s*$", txt)
                if mb:
                    badge = mb.group(1)
                    txt = txt[:mb.start()].rstrip()
                refs.append((n, txt, badge))
            continue

        # ---- 标题（支持 1-6 级）----
        mh = HEADING_RE.match(line)
        if mh:
            flush_table()
            flush_list()
            flush_bq()
            level = len(mh.group(1))
            text = mh.group(2).strip()
            if not text:
                continue
            if level == 1 and not title and "参考文献" not in text:
                title = text
                continue
            h = min(level, 4)
            anchors["sec"] += 1
            aid = f"sec-{anchors['sec']}"
            body.append(f'<h{h} id="{aid}" class="h-{h}">{inline(text)}</h{h}>')
            if h in (2, 3):
                toc.append((h, text, aid))
            continue

        # ---- 表格 ----
        s = line.strip()
        if s.startswith("|") and s.endswith("|") and len(s) > 1:
            flush_list()
            flush_bq()
            cells = [c.strip() for c in s.strip("|").split("|")]
            if cells and all(SEP_CELL_RE.match(c) for c in cells):
                table_align = [_align_of(c) for c in cells]
            else:
                table_buf.append(s)
            continue

        # ---- 块引用 ----
        if s.startswith(">"):
            flush_table()
            flush_list()
            bq_buf.append(s.lstrip(">").strip())
            continue

        # ---- 列表 ----
        m_ul = re.match(r"^\s*[-*]\s+(.*)$", line)
        m_ol = re.match(r"^\s*(\d+)\.\s+(.*)$", line)
        if m_ul:
            flush_table()
            flush_bq()
            close_list("ul")
            body.append(f"<li>{inline(m_ul.group(1))}</li>")
            continue
        if m_ol:
            flush_table()
            flush_bq()
            start = int(m_ol.group(1))
            if list_kind != "ol":
                close_list("ol", start)
            body.append(f"<li>{inline(m_ol.group(2))}</li>")
            continue

        # ---- 空行：结束块 ----
        if not s:
            flush_table()
            flush_list()
            flush_bq()
            continue

        # ---- 普通段落 ----
        flush_table()
        flush_list()
        flush_bq()
        body.append(f"<p>{inline(line)}</p>")

    flush_table()
    flush_list()
    flush_bq()
    if fences is not None:  # 未闭合的围栏块兜底
        if fences[0] == "chart":
            body.append('<figure class="chart">' + render_chart_block("\n".join(fences[1])) + "</figure>")

    return title, toc, body, refs, ref_note


# ---------- 引用校验 ----------

BLOCK_DOMAINS = [
    "baijiahao.baidu.com", "mbd.baidu.com", "toutiao.com", "wukong.com",
    "uc.cn", "m.uc.cn", "360kuai.com", "so.com",
]
CAUTION_DOMAINS = [
    "sohu.com", "163.com", "sina.com.cn", "zhihu.com", "csdn.net",
    "51cto.com", "xueqiu.com", "jiemian.com",
]


def _expand_cites(s):
    out = set()
    for grp in CITE_RE.findall(s):
        for p in grp.split(","):
            p = p.strip()
            if "-" in p:
                a, b = p.split("-", 1)
                try:
                    out.update(str(i) for i in range(int(a), int(b) + 1))
                except ValueError:
                    pass
            elif p.isdigit():
                out.add(str(int(p)))
    return out


def lint(md_text, refs):
    """返回 (errors, warns, stats)。errors/warns 为字符串列表。"""
    errors, warns = [], []
    body_md = re.split(r"^\s*#{1,3}\s*(?:参考文献|References)\s*$", md_text, flags=re.M)[0]
    body_md = re.sub(r"```.*?```", "", body_md, flags=re.S)  # 排除代码块内的 [1]
    cited = _expand_cites(body_md)
    ref_ids = [n for n, _, _ in refs]
    ref_set = set(ref_ids)

    dangling = sorted(cited - ref_set, key=int)
    unused = sorted(ref_set - cited, key=int)
    if dangling:
        errors.append(f"正文引用了文献表中不存在的编号: [{', '.join(dangling)}]")
    if unused:
        warns.append(f"文献表中有未被正文引用的条目: [{', '.join(unused)}]")

    dup = sorted({n for n in ref_ids if ref_ids.count(n) > 1}, key=int)
    if dup:
        errors.append(f"参考文献编号重复: [{', '.join(dup)}]")

    nums = sorted({int(n) for n in ref_ids if n.isdigit()})
    if nums:
        missing = [str(i) for i in range(1, max(nums) + 1) if i not in set(nums)]
        if missing:
            errors.append(f"参考文献编号断号，缺失: [{', '.join(missing)}]")

    for n, txt, badge in refs:
        if "EB/OL" in txt or "http" in txt:
            if not re.search(r"https?://", txt):
                errors.append(f"[{n}] 网络文献缺少 URL")
            if not re.search(r"\[\d{4}-\d{2}-\d{2}\]", txt):
                warns.append(f"[{n}] 网络文献缺少引用日期 [YYYY-MM-DD]")
        if not badge:
            warns.append(f"[{n}] 缺少可信级别徽标 [A级]/[B级]")
        for d in BLOCK_DOMAINS:
            if d in txt:
                errors.append(f"[{n}] 命中低质信源域名 {d}，须替换为白名单来源")
        for d in CAUTION_DOMAINS:
            if d in txt and d not in BLOCK_DOMAINS:
                warns.append(f"[{n}] 来源域名 {d} 需人工确认是否属于白名单")

    stats = {
        "cited": len(cited),
        "refs": len(refs),
        "A": sum(1 for _, _, b in refs if b == "A"),
        "B": sum(1 for _, _, b in refs if b == "B"),
    }
    return errors, warns, stats


# ---------- 输出模板 ----------

CSS = """
:root{--ink:#1F2430;--muted:#5F5E5A;--line:#E5E3DC;--accent:#185FA5;--bg:#FFFFFF}
*{box-sizing:border-box}
body{margin:0;background:#F4F3EF;color:var(--ink);font-family:"PingFang SC","Microsoft YaHei","Noto Sans SC",-apple-system,sans-serif;line-height:1.75}
.report{max-width:920px;margin:0 auto;background:var(--bg);padding:56px 64px 72px}
h1{font-size:28px;font-weight:700;margin:0 0 8px;letter-spacing:.5px}
h2{font-size:21px;font-weight:600;margin:44px 0 14px;padding-bottom:8px;border-bottom:2px solid var(--line)}
h3{font-size:17px;font-weight:600;margin:28px 0 10px}
h4{font-size:15px;font-weight:600;margin:22px 0 8px;color:#2C2C2A}
p{margin:12px 0;text-align:justify}
strong{font-weight:600}
code{font-family:"SFMono-Regular",Consolas,monospace;font-size:13px;background:#F1EFE8;padding:1px 5px;border-radius:4px}
pre.code{background:#F8F7F4;border:1px solid var(--line);border-radius:8px;padding:14px 16px;overflow-x:auto;margin:16px 0}
pre.code code{background:none;padding:0;font-size:12.5px;line-height:1.6}
blockquote.quote{margin:16px 0;padding:10px 18px;background:#F8F7F4;border-left:3px solid var(--line);border-radius:0 8px 8px 0;color:#444441}
blockquote.quote p{margin:6px 0}
sup.cite{font-size:11px;line-height:0}
sup.cite a{color:var(--accent);text-decoration:none}
sup.cite a:hover{text-decoration:underline}
.tbl-wrap{overflow-x:auto;margin:16px 0}
table{width:100%;font-size:14px;border-collapse:collapse}
th{background:#F1EFE8;font-weight:600;text-align:left;padding:9px 12px;border:1px solid var(--line)}
td{padding:8px 12px;border:1px solid var(--line)}
ul,ol{margin:12px 0;padding-left:26px}
li{margin:6px 0}
.chart{margin:20px 0;text-align:center;background:#FDFDFB;border:1px solid var(--line);border-radius:10px;padding:16px}
.toc{background:#F8F7F4;border:1px solid var(--line);border-radius:10px;padding:20px 26px;margin:28px 0 8px}
.toc-title{font-size:15px;font-weight:600;margin:0 0 10px}
.toc ul{list-style:none;padding-left:0;margin:0}
.toc ul ul{padding-left:22px}
.toc a{color:var(--ink);text-decoration:none}
.toc a:hover{color:var(--accent)}
.toc .lvl-2{font-weight:500}
.toc .lvl-3{padding-left:20px;font-size:14px;color:var(--muted)}
.refs ol{list-style:none;padding-left:0;margin-left:0}
.refs li{margin:10px 0;font-size:14px;color:#2C2C2A;position:relative;padding-left:2px}
.ref-id{display:inline-block;min-width:26px;font-weight:600;color:var(--accent)}
.refnote{margin:18px 0 0;padding:14px 18px;background:#F8F7F4;border-left:3px solid var(--accent);border-radius:0 8px 8px 0;font-size:13px;color:#3C3489}
.refnote p{margin:6px 0}
.badge{display:inline-block;font-size:11px;padding:1px 8px;border-radius:99px;margin-left:8px;vertical-align:1px}
.badge-A{background:#E1F5EE;color:#0F6E56}
.badge-B{background:#E6F1FB;color:#185FA5}
.footer{margin-top:56px;padding-top:16px;border-top:1px solid var(--line);font-size:12px;color:var(--muted);text-align:center}
@media print{body{background:#fff}.report{max-width:none;padding:24px}.toc{break-after:auto}}
"""


def build_html(md_text, custom_title=None):
    title, toc, body, refs, ref_note = convert(md_text)
    title = custom_title or title or "研究报告"

    toc_html = ""
    if toc:
        items = "".join(f'<li class="lvl-{lvl}"><a href="#{aid}">{esc(t)}</a></li>'
                        for lvl, t, aid in toc)
        toc_html = f'<nav class="toc"><p class="toc-title">目录</p><ul>{items}</ul></nav>'

    refs_html = ""
    if refs:
        items = "".join(
            f'<li id="ref-{n}"><span class="ref-id">[{n}]</span>{esc(txt)}'
            + (f'<span class="badge badge-{lv}">{lv}级</span>' if lv else "")
            + "</li>"
            for n, txt, lv in refs)
        note_html = "".join(f"<p>{esc(t)}</p>" for t in ref_note)
        bq = f'<blockquote class="refnote">{note_html}</blockquote>' if note_html else ""
        refs_html = f'<section class="refs"><h2>参考文献</h2><ol>{items}</ol>{bq}</section>'

    return (f'<!DOCTYPE html>\n<html lang="zh-CN">\n<head>\n<meta charset="utf-8">\n'
            f'<meta name="viewport" content="width=device-width, initial-scale=1">\n'
            f'<title>{esc(title)}</title>\n<style>{CSS}</style>\n</head>\n<body>\n'
            f'<div class="report">\n<header><h1>{esc(title)}</h1></header>\n'
            f'{toc_html}\n<main>\n{"".join(body)}\n</main>\n{refs_html}\n'
            f'<footer class="footer">由 storm-report（STORM 方法）生成 · 引用遵循 GB/T 7714-2015</footer>\n'
            f'</div>\n</body>\n</html>')


def main():
    ap = argparse.ArgumentParser(description="把 storm-report 的 Markdown 报告构建为独立 HTML")
    ap.add_argument("report_md", help="输入的 Markdown 报告路径")
    ap.add_argument("-o", "--output", help="输出 HTML 路径（默认与输入同名 .html）")
    ap.add_argument("--title", help="自定义报告标题（默认取首个 # 标题）")
    ap.add_argument("--lint", action="store_true", help="只做引用校验，不生成 HTML")
    ap.add_argument("--strict", action="store_true", help="构建后若校验发现 ERROR 则以退出码 1 结束")
    args = ap.parse_args()

    with open(args.report_md, "r", encoding="utf-8") as f:
        md = f.read()

    _, _, _, refs, _ = convert(md)
    errors, warns, stats = lint(md, refs)

    print("[引用校验] 正文引用 {} 处 | 参考文献 {} 条（A级 {} / B级 {}）".format(
        stats["cited"], stats["refs"], stats["A"], stats["B"]))
    for w in warns:
        print(f"  [WARN]  {w}")
    for e in errors:
        print(f"  [ERROR] {e}")
    if not warns and not errors:
        print("  [OK] 无断号、无悬空引用、格式与信源合规")

    if args.lint:
        sys.exit(1 if errors else 0)

    out = args.output or (os.path.splitext(args.report_md)[0] + ".html")
    with open(out, "w", encoding="utf-8") as f:
        f.write(build_html(md, args.title))
    print(f"已生成报告: {out}（{os.path.getsize(out)} bytes）")

    sys.exit(1 if (args.strict and errors) else 0)


if __name__ == "__main__":
    main()
