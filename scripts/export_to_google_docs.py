#!/usr/bin/env python3
"""
Convert GKE_GPU_WORKLOAD_INIT_TEST_GUIDE.md to Google-Docs-Ready Rich Styled HTML.
Generates an HTML file specifically structured with inline styles specifically right for optimal Google Docs import and instantaneous copy-paste execution.
"""
import sys
import re
import html
import os

def convert_md_to_gdocs_html(md_path, html_path):
    if not os.path.exists(md_path):
        print(f"[!] Error: Target markdown source file '{md_path}' does not exist.")
        sys.exit(1)

    with open(md_path, "r", encoding="utf-8") as f:
        lines = f.readlines()

    out_lines = [
        "<!DOCTYPE html>",
        "<html>",
        "<head>",
        "<meta charset='utf-8'>",
        "<title>Complete Step-by-Step Initialization & Testing Guide: Hosting Multi-GPU AI Hypercomputer Workloads on GKE</title>",
        "<style>",
        "body { font-family: 'Arial', sans-serif; font-size: 11pt; line-height: 1.6; color: #222222; max-width: 8.5in; margin: 1in auto; padding: 0 1rem; }",
        "h1 { color: #1a73e8; font-size: 20pt; border-bottom: 2px solid #e0e0e0; padding-bottom: 8px; margin-top: 24px; font-weight: bold; }",
        "h2 { color: #1a73e8; font-size: 15pt; margin-top: 22px; margin-bottom: 10px; border-bottom: 1px solid #eeeeee; padding-bottom: 4px; font-weight: bold; }",
        "h3 { color: #202124; font-size: 13pt; margin-top: 18px; margin-bottom: 8px; font-weight: bold; }",
        "h4 { color: #5f6368; font-size: 11.5pt; margin-top: 14px; margin-bottom: 6px; font-weight: bold; }",
        "p { margin-bottom: 10px; }",
        "pre { background-color: #f8f9fa; border: 1px solid #dadce0; border-radius: 6px; padding: 12px; font-family: 'Courier New', monospace; font-size: 9.5pt; white-space: pre-wrap; word-break: break-all; margin-bottom: 14px; }",
        "code { background-color: #f1f3f4; color: #d93025; font-family: 'Courier New', monospace; font-size: 9.5pt; padding: 2px 4px; border-radius: 4px; }",
        "pre code { background-color: transparent; color: #202124; padding: 0; }",
        "table { width: 100%; border-collapse: collapse; margin-bottom: 18px; font-size: 10pt; }",
        "th { background-color: #e8f0fe; color: #1a73e8; border: 1px solid #dadce0; padding: 10px; text-align: left; font-weight: bold; }",
        "td { border: 1px solid #dadce0; padding: 9px; vertical-align: top; }",
        "ul, ol { margin-top: 4px; margin-bottom: 12px; padding-left: 24px; }",
        "li { margin-bottom: 6px; }",
        ".alert-caution { border-left: 5px solid #d93025; background-color: #fce8e6; padding: 12px; border-radius: 0 4px 4px 0; margin-bottom: 14px; color: #3c4043; }",
        ".mermaid-block { border-left: 4px solid #1a73e8; background-color: #e8f0fe; padding: 12px; border-radius: 4px; font-family: 'Courier New', monospace; font-size: 9pt; margin-bottom: 14px; }",
        "hr { border: 0; border-top: 1px solid #dadce0; margin: 22px 0; }",
        "</style>",
        "</head>",
        "<body>"
    ]

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

    def parse_inline(text):
        # Bold: **text**
        text = re.sub(r'\*\*(.+?)\*\*', r'<b>\1</b>', text)
        # Inline Code: `code`
        text = re.sub(r'`(.+?)`', r'<code>\1</code>', text)
        # Markdown links: [text](file://... or https://...)
        text = re.sub(r'\[(.+?)\]\(([^\)]+)\)', r'<a href="\2" style="color:#1a73e8;text-decoration:none;"><b>\1</b></a>', text)
        return text

    i = 0
    while i < len(lines):
        line = lines[i].rstrip()
        
        # 1. Code blocks and Mermaid blocks
        if line.startswith("```"):
            close_tags = close_lists() + close_table()
            out_lines.extend(close_tags)
            
            if in_pre or in_mermaid:
                if in_mermaid:
                    out_lines.append("</div>")
                    in_mermaid = False
                else:
                    out_lines.append("</code></pre>")
                    in_pre = False
            else:
                lang = line[3:].strip()
                if lang.lower() == "mermaid":
                    out_lines.append("<div class='mermaid-block'><b>[Mermaid Structural Flowchart Definition]</b><br><br>")
                    in_mermaid = True
                else:
                    out_lines.append(f"<pre><code class='language-{lang}'>")
                    in_pre = True
            i += 1
            continue

        if in_pre:
            out_lines.append(html.escape(line) + "\n")
            i += 1
            continue
        elif in_mermaid:
            out_lines.append(html.escape(line).replace(" ", "&nbsp;") + "<br>")
            i += 1
            continue

        # 2. Horizontal separation lines
        if line == "---" or line == "***":
            out_lines.extend(close_lists() + close_table())
            out_lines.append("<hr />")
            i += 1
            continue

        # 3. Headings
        if line.startswith("# ") or line.startswith("## ") or line.startswith("### ") or line.startswith("#### "):
            out_lines.extend(close_lists() + close_table())
            match = re.match(r'^(#{1,4})\s+(.*)', line)
            if match:
                level = len(match.group(1))
                content = parse_inline(match.group(2))
                out_lines.append(f"<h{level}>{content}</h{level}>")
            i += 1
            continue

        # 4. Tables
        if line.startswith("|") and line.endswith("|"):
            if not in_table:
                out_lines.extend(close_lists())
                out_lines.append("<table>")
                in_table = True
                cols = [c.strip() for c in line.split("|")[1:-1]]
                out_lines.append("<thead><tr>" + "".join(f"<th>{parse_inline(c)}</th>" for c in cols) + "</tr></thead><tbody>")
                if i + 1 < len(lines) and lines[i+1].strip().startswith("|") and "-" in lines[i+1]:
                    i += 2
                    continue
            else:
                if "-" not in line or re.sub(r'[\s\|\-:]', '', line) != "":
                    cols = [c.strip() for c in line.split("|")[1:-1]]
                    out_lines.append("<tr>" + "".join(f"<td>{parse_inline(c)}</td>" for c in cols) + "</tr>")
            i += 1
            continue
        else:
            if in_table:
                out_lines.extend(close_table())

        # 5. Alert callouts
        if line.startswith("> [!CAUTION]") or line.startswith("> [!WARNING]"):
            out_lines.extend(close_lists() + close_table())
            out_lines.append("<div class='alert-caution'><b>⚠️ CAUTION & SYSTEM PREREQUISITE WARNING:</b><br>")
            i += 1
            while i < len(lines) and lines[i].strip().startswith(">"):
                alert_text = re.sub(r'^>\s*', '', lines[i].strip())
                out_lines.append(parse_inline(alert_text) + " ")
                i += 1
            out_lines.append("</div>")
            continue

        if line.startswith("> "):
            out_lines.append("<blockquote style='border-left: 4px solid #dadce0; padding-left: 12px; color: #5f6368; margin: 12px 0;'>" + parse_inline(re.sub(r'^>\s*', '', line)) + "</blockquote>")
            i += 1
            continue

        # 6. Unordered Lists
        if re.match(r'^\s*[-*]\s+(.*)', line):
            if not in_ul:
                if in_ol:
                    out_lines.append("</ol>")
                    in_ol = False
                out_lines.append("<ul>")
                in_ul = True
            content = re.sub(r'^\s*[-*]\s+', '', line)
            out_lines.append(f"<li>{parse_inline(content)}</li>")
            i += 1
            continue
        else:
            if in_ul and line.strip() == "":
                if i + 1 < len(lines) and re.match(r'^\s*[-*]\s+', lines[i+1]):
                    i += 1
                    continue
                else:
                    out_lines.append("</ul>")
                    in_ul = False

        # 7. Ordered Lists
        if re.match(r'^\s*\d+\.\s+(.*)', line):
            if not in_ol:
                if in_ul:
                    out_lines.append("</ul>")
                    in_ul = False
                out_lines.append("<ol>")
                in_ol = True
            content = re.sub(r'^\s*\d+\.\s+', '', line)
            out_lines.append(f"<li>{parse_inline(content)}</li>")
            i += 1
            continue
        else:
            if in_ol and line.strip() == "":
                if i + 1 < len(lines) and re.match(r'^\s*\d+\.\s+', lines[i+1]):
                    i += 1
                    continue
                else:
                    out_lines.append("</ol>")
                    in_ol = False

        # 8. Regular paragraphs
        if line.strip() != "":
            out_lines.append(f"<p>{parse_inline(line)}</p>")
        
        i += 1

    out_lines.extend(close_lists() + close_table())
    out_lines.append("</body></html>")

    with open(html_path, "w", encoding="utf-8") as out_f:
        out_f.write('\n'.join(out_lines))
    print(f"[+] Successfully generated Google-Docs optimized HTML format: '{html_path}'")

if __name__ == "__main__":
    src_md = "/Users/elideng/hypercomputer-training-jobs/GKE_GPU_WORKLOAD_INIT_TEST_GUIDE.md"
    dst_html = "/Users/elideng/hypercomputer-training-jobs/GKE_GPU_WORKLOAD_INIT_TEST_GUIDE_Google_Docs_Export.html"
    convert_md_to_gdocs_html(src_md, dst_html)
