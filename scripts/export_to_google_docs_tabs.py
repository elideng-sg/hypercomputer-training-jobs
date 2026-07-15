#!/usr/bin/env python3
"""
Generate an interactive multi-tab HTML exporter across GKE_GPU_WORKLOAD_INIT_TEST_GUIDE.md
Empowers rapid 1-click rich copying of each individual Part straight directly into distinct Google Docs Tabs.
"""
import sys
import re
import html
import os

def parse_inline(text):
    text = text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
    text = re.sub(r'\*\*(.+?)\*\*', r'<b>\1</b>', text)
    text = re.sub(r'`(.+?)`', r'<code>\1</code>', text)
    text = re.sub(r'\[(.+?)\]\(([^\)]+)\)', r'<a href="\2" style="color:#1a73e8;text-decoration:none;"><b>\1</b></a>', text)
    return text

def convert_section_to_html(lines):
    out = []
    in_pre = False
    in_mermaid = False
    in_table = False
    in_ul = False
    in_ol = False

    def close_lists():
        nonlocal in_ul, in_ol
        tags = []
        if in_ul:
            tags.append("</ul>")
            in_ul = False
        if in_ol:
            tags.append("</ol>")
            in_ol = False
        return tags

    def close_table():
        nonlocal in_table
        if in_table:
            in_table = False
            return ["</tbody></table>"]
        return []

    i = 0
    while i < len(lines):
        line = lines[i].rstrip()
        
        if line.startswith("```"):
            out.extend(close_lists() + close_table())
            if in_pre or in_mermaid:
                if in_mermaid:
                    out.append("</div>")
                    in_mermaid = False
                else:
                    out.append("</code></pre>")
                    in_pre = False
            else:
                lang = line[3:].strip()
                if lang.lower() == "mermaid":
                    out.append("<div class='mermaid-block'><b>[Mermaid Structural Flowchart Definition]</b><br><br>")
                    in_mermaid = True
                else:
                    out.append(f"<pre><code class='language-{lang}'>")
                    in_pre = True
            i += 1
            continue

        if in_pre:
            out.append(html.escape(line) + "\n")
            i += 1
            continue
        elif in_mermaid:
            out.append(html.escape(line).replace(" ", "&nbsp;") + "<br>")
            i += 1
            continue

        if line == "---" or line == "***":
            out.extend(close_lists() + close_table())
            out.append("<hr />")
            i += 1
            continue

        if line.startswith("# ") or line.startswith("## ") or line.startswith("### ") or line.startswith("#### "):
            out.extend(close_lists() + close_table())
            match = re.match(r'^(#{1,4})\s+(.*)', line)
            if match:
                level = len(match.group(1))
                content = parse_inline(match.group(2))
                out.append(f"<h{level}>{content}</h{level}>")
            i += 1
            continue

        if line.startswith("|") and line.endswith("|"):
            if not in_table:
                out.extend(close_lists())
                out.append("<table>")
                in_table = True
                cols = [c.strip() for c in line.split("|")[1:-1]]
                out.append("<thead><tr>" + "".join(f"<th>{parse_inline(c)}</th>" for c in cols) + "</tr></thead><tbody>")
                if i + 1 < len(lines) and lines[i+1].strip().startswith("|") and "-" in lines[i+1]:
                    i += 2
                    continue
            else:
                if "-" not in line or re.sub(r'[\s\|\-:]', '', line) != "":
                    cols = [c.strip() for c in line.split("|")[1:-1]]
                    out.append("<tr>" + "".join(f"<td>{parse_inline(c)}</td>" for c in cols) + "</tr>")
            i += 1
            continue
        else:
            if in_table:
                out.extend(close_table())

        if line.startswith("> [!CAUTION]") or line.startswith("> [!WARNING]"):
            out.extend(close_lists() + close_table())
            out.append("<div class='alert-caution'><b>⚠️ CAUTION & SYSTEM PREREQUISITE WARNING:</b><br>")
            i += 1
            while i < len(lines) and lines[i].strip().startswith(">"):
                alert_text = re.sub(r'^>\s*', '', lines[i].strip())
                out.append(parse_inline(alert_text) + " ")
                i += 1
            out.append("</div>")
            continue

        if re.match(r'^\s*[-*]\s+(.*)', line):
            if not in_ul:
                if in_ol:
                    out.append("</ol>")
                    in_ol = False
                out.append("<ul>")
                in_ul = True
            content = re.sub(r'^\s*[-*]\s+', '', line)
            out.append(f"<li>{parse_inline(content)}</li>")
            i += 1
            continue
        else:
            if in_ul and line.strip() == "":
                if i + 1 < len(lines) and re.match(r'^\s*[-*]\s+', lines[i+1]):
                    i += 1
                    continue
                else:
                    out.append("</ul>")
                    in_ul = False

        if re.match(r'^\s*\d+\.\s+(.*)', line):
            if not in_ol:
                if in_ul:
                    out.append("</ul>")
                    in_ul = False
                out.append("<ol>")
                in_ol = True
            content = re.sub(r'^\s*\d+\.\s+', '', line)
            out.append(f"<li>{parse_inline(content)}</li>")
            i += 1
            continue
        else:
            if in_ol and line.strip() == "":
                if i + 1 < len(lines) and re.match(r'^\s*\d+\.\s+', lines[i+1]):
                    i += 1
                    continue
                else:
                    out.append("</ol>")
                    in_ol = False

        if line.strip() != "":
            out.append(f"<p>{parse_inline(line)}</p>")
        
        i += 1

    out.extend(close_lists() + close_table())
    return "\n".join(out)

def split_into_tabs(md_path):
    with open(md_path, "r", encoding="utf-8") as f:
        content = f.read()

    parts = re.split(r'\n(?=## Part [I|V|X]+:)', content)
    tabs = []
    
    # Overview
    tabs.append(("Overview & Index", parts[0].strip().split("\n")))
    for p in parts[1:]:
        header_line = p.split("\n")[0]
        match = re.match(r'## (Part [^:]+:\s*.*?)$', header_line)
        title = match.group(1) if match else "Document Part"
        tabs.append((title, p.strip().split("\n")))
    
    return tabs

def generate_tabbed_html(src, dst):
    tabs_data = split_into_tabs(src)
    
    html_header = """<!DOCTYPE html>
<html>
<head>
<meta charset='utf-8'>
<title>Google Docs Interactive Tabs Copy Center: GKE GPU Workload Guide</title>
<style>
body { font-family: 'Arial', sans-serif; font-size: 11pt; line-height: 1.6; color: #222222; margin: 0; padding: 20px; background-color: #f1f3f4; }
.header-box { background: #1a73e8; color: #ffffff; padding: 20px 30px; border-radius: 8px; max-width: 1000px; margin: 0 auto 20px auto; box-shadow: 0 2px 6px rgba(0,0,0,0.2); }
.header-box h1 { margin: 0 0 10px 0; font-size: 22pt; color: #ffffff; border: none; padding: 0; }
.header-box p { margin: 0; font-size: 11pt; opacity: 0.95; }
.tabs-container { max-width: 1000px; margin: 0 auto; display: flex; flex-direction: row; }
.tab-buttons { width: 260px; flex-shrink: 0; background: #ffffff; border-radius: 8px 0 0 8px; border: 1px solid #dadce0; border-right: none; overflow: hidden; }
.tab-btn { display: block; width: 100%; padding: 14px 18px; text-align: left; background: transparent; border: none; border-bottom: 1px solid #eeeeee; font-size: 11pt; font-weight: bold; color: #5f6368; cursor: pointer; transition: background 0.2s, color 0.2s; }
.tab-btn:hover { background: #f8f9fa; color: #1a73e8; }
.tab-btn.active { background: #e8f0fe; color: #1a73e8; border-left: 5px solid #1a73e8; }
.tab-panels { flex-grow: 1; background: #ffffff; border: 1px solid #dadce0; border-radius: 0 8px 8px 8px; padding: 30px; box-shadow: 0 2px 6px rgba(0,0,0,0.05); }
.tab-panel { display: none; }
.tab-panel.active { display: block; }
.copy-toolbar { background: #e8f0fe; border: 1px solid #dadce0; border-radius: 6px; padding: 12px 18px; margin-bottom: 24px; display: flex; align-items: center; justify-content: space-between; }
.copy-toolbar span { font-weight: bold; color: #1a73e8; font-size: 11pt; }
.copy-btn { background: #1a73e8; color: #ffffff; border: none; border-radius: 4px; padding: 10px 20px; font-size: 10.5pt; font-weight: bold; cursor: pointer; transition: background 0.2s; }
.copy-btn:hover { background: #1557b0; }
.content-box { max-width: 100%; }

/* Document inner content styling specifically targeted for clean rich clipboard transfer */
.content-box h1 { color: #1a73e8; font-size: 20pt; border-bottom: 2px solid #e0e0e0; padding-bottom: 8px; margin-top: 24px; }
.content-box h2 { color: #1a73e8; font-size: 15pt; margin-top: 22px; margin-bottom: 10px; border-bottom: 1px solid #eeeeee; padding-bottom: 4px; }
.content-box h3 { color: #202124; font-size: 13pt; margin-top: 18px; margin-bottom: 8px; }
.content-box h4 { color: #5f6368; font-size: 11.5pt; margin-top: 14px; margin-bottom: 6px; }
.content-box pre { background-color: #f8f9fa; border: 1px solid #dadce0; border-radius: 6px; padding: 12px; font-family: 'Courier New', monospace; font-size: 9.5pt; white-space: pre-wrap; word-break: break-all; margin-bottom: 14px; }
.content-box code { background-color: #f1f3f4; color: #d93025; font-family: 'Courier New', monospace; font-size: 9.5pt; padding: 2px 4px; border-radius: 4px; }
.content-box pre code { background-color: transparent; color: #202124; padding: 0; }
.content-box table { width: 100%; border-collapse: collapse; margin-bottom: 18px; font-size: 10pt; }
.content-box th { background-color: #e8f0fe; color: #1a73e8; border: 1px solid #dadce0; padding: 10px; text-align: left; }
.content-box td { border: 1px solid #dadce0; padding: 9px; vertical-align: top; }
.content-box ul, .content-box ol { margin-top: 4px; margin-bottom: 12px; padding-left: 24px; }
.content-box li { margin-bottom: 6px; }
.alert-caution { border-left: 5px solid #d93025; background-color: #fce8e6; padding: 12px; border-radius: 0 4px 4px 0; margin-bottom: 14px; }
.mermaid-block { border-left: 4px solid #1a73e8; background-color: #e8f0fe; padding: 12px; border-radius: 4px; font-family: 'Courier New', monospace; font-size: 9pt; margin-bottom: 14px; }
</style>
<script>
function showTab(idx) {
    var btns = document.querySelectorAll('.tab-btn');
    var panels = document.querySelectorAll('.tab-panel');
    for(var i=0; i<btns.length; i++) {
        btns[i].className = 'tab-btn' + (i == idx ? ' active' : '');
        panels[i].className = 'tab-panel' + (i == idx ? ' active' : '');
    }
}

function selectAndCopy(containerId, btnElement) {
    var range = document.createRange();
    var elem = document.getElementById(containerId);
    range.selectNodeContents(elem);
    var sel = window.getSelection();
    sel.removeAllRanges();
    sel.addRange(range);
    document.execCommand('copy');
    sel.removeAllRanges();
    
    var origText = btnElement.innerText;
    btnElement.innerText = '✅ Copied Rich Formatted Part to Clipboard!';
    btnElement.style.background = '#0d652d';
    setTimeout(function() {
        btnElement.innerText = origText;
        btnElement.style.background = '#1a73e8';
    }, 2500);
}
</script>
</head>
<body>
<div class="header-box">
    <h1>📋 Google Docs Interactive Tabs Transfer Hub</h1>
    <p><b>Instructions:</b> Click any section tab below right on the left pane, click the blue <b>'Copy Part for Google Doc Tab'</b> button, switch to your Google Doc (<a href="https://doc.new" target="_blank" style="color:#ffffff;text-decoration:underline;"><b>doc.new</b></a>), click <b>+ (Add Tab)</b> right across the document sidebar, and press <b>Cmd + V</b> right right to drop the styled content into place!</p>
</div>
<div class="tabs-container">
    <div class="tab-buttons">
"""
    
    for idx, (title, _) in enumerate(tabs_data):
        short_title = title.split("—")[0].strip()
        if len(short_title) > 35:
            short_title = short_title[:32] + "..."
        active_cls = " active" if idx == 0 else ""
        html_header += f'<button class="tab-btn{active_cls}" onclick="showTab({idx})">📑 {html.escape(short_title)}</button>\n'
        
    html_header += '    </div>\n    <div class="tab-panels">\n'
    
    for idx, (title, lines) in enumerate(tabs_data):
        active_cls = " active" if idx == 0 else ""
        panel_html = convert_section_to_html(lines)
        html_header += f'''        <div class="tab-panel{active_cls}">
            <div class="copy-toolbar">
                <span>📑 Current Tab: {html.escape(title)}</span>
                <button class="copy-btn" onclick="selectAndCopy('content-part-{idx}', this)">📋 Copy Part {idx if idx>0 else 'Overview'} for Google Doc Tab</button>
            </div>
            <div class="content-box" id="content-part-{idx}">
                {panel_html}
            </div>
        </div>
'''
    html_header += "    </div>\n</div>\n</body>\n</html>"
    
    with open(dst, "w", encoding="utf-8") as f:
        f.write(html_header)
    print(f"[+] Multi-tab Google Docs copy center exported right right across right across: '{dst}'")

if __name__ == "__main__":
    src = "/Users/elideng/hypercomputer-training-jobs/GKE_GPU_WORKLOAD_INIT_TEST_GUIDE.md"
    dst = "/Users/elideng/hypercomputer-training-jobs/GKE_GPU_WORKLOAD_INIT_TEST_GUIDE_Tabbed_Exporter.html"
    generate_tabbed_html(src, dst)
