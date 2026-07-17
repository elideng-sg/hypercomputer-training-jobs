#!/usr/bin/env python3
"""
Convert Markdown files to self-contained Google-Docs-ready HTML.

Usage:
    python3 export_to_google_docs.py <input1.md> [input2.md ...] -o <output.html>

Features:
- Concatenates multiple Markdown inputs into a single HTML with table of contents
- Inlines local SVG images (so the export is self-contained for pasting into Google Docs)
- Clean, readable CSS styling
- No external dependencies (uses Python standard library only)
"""
import argparse
import html
import os
import re
import sys
from pathlib import Path


def parse_inline(text):
    """Parse inline Markdown: bold, code, links."""
    # Escape HTML
    text = html.escape(text)
    # Bold: **text** or __text__
    text = re.sub(r'\*\*(.+?)\*\*', r'<strong>\1</strong>', text)
    text = re.sub(r'__(.+?)__', r'<strong>\1</strong>', text)
    # Italic: *text* or _text_ (but not in middle of words)
    text = re.sub(r'(?<!\w)\*([^\*]+?)\*(?!\w)', r'<em>\1</em>', text)
    text = re.sub(r'(?<!\w)_([^_]+?)_(?!\w)', r'<em>\1</em>', text)
    # Code: `code`
    text = re.sub(r'`([^`]+)`', r'<code>\1</code>', text)
    # Links: [text](url)
    text = re.sub(r'\[([^\]]+)\]\(([^\)]+)\)', r'<a href="\2">\1</a>', text)
    return text


def inline_svg_images(html_content, base_dir):
    """
    Replace <img src="...NAME.svg"> tags with inline SVG content.
    Resolves paths relative to base_dir.
    """
    def replace_svg(match):
        src = match.group(1)
        # Handle both absolute and relative paths
        if not os.path.isabs(src):
            svg_path = os.path.normpath(os.path.join(base_dir, src))
        else:
            svg_path = src

        if os.path.exists(svg_path) and svg_path.endswith('.svg'):
            try:
                with open(svg_path, 'r', encoding='utf-8') as f:
                    svg_content = f.read()
                    # Strip XML declaration if present
                    svg_content = re.sub(r'<\?xml[^>]+\?>\s*', '', svg_content)
                    return svg_content
            except Exception as e:
                print(f"[!] Warning: Failed to inline {svg_path}: {e}", file=sys.stderr)
                return match.group(0)  # Return original tag if read fails
        else:
            # Not an SVG or doesn't exist, leave as-is
            return match.group(0)

    # Match <img src="..."> tags
    html_content = re.sub(r'<img\s+(?:[^>]*?\s+)?src="([^"]+\.svg)"[^>]*?/?>', replace_svg, html_content)
    return html_content


def convert_markdown_to_html(markdown_content):
    """
    Convert Markdown to HTML using a simple parser.
    Handles: headings, paragraphs, lists, code blocks, tables, links, bold, italic, code.
    """
    lines = markdown_content.split('\n')
    html_lines = []

    in_code_block = False
    in_list = False
    in_ordered_list = False
    in_table = False
    table_headers = []
    code_lang = ''

    i = 0
    while i < len(lines):
        line = lines[i]
        stripped = line.strip()

        # Code blocks
        if stripped.startswith('```'):
            if in_code_block:
                html_lines.append('</code></pre>')
                in_code_block = False
                code_lang = ''
            else:
                code_lang = stripped[3:].strip()
                html_lines.append(f'<pre><code class="language-{code_lang}">')
                in_code_block = True
            i += 1
            continue

        if in_code_block:
            html_lines.append(html.escape(line))
            i += 1
            continue

        # Headings
        heading_match = re.match(r'^(#{1,6})\s+(.+)$', stripped)
        if heading_match:
            if in_list:
                html_lines.append('</ul>')
                in_list = False
            if in_ordered_list:
                html_lines.append('</ol>')
                in_ordered_list = False
            if in_table:
                html_lines.append('</tbody></table>')
                in_table = False

            level = len(heading_match.group(1))
            content = parse_inline(heading_match.group(2))
            html_lines.append(f'<h{level}>{content}</h{level}>')
            i += 1
            continue

        # Horizontal rule
        if stripped in ['---', '***', '___']:
            if in_list:
                html_lines.append('</ul>')
                in_list = False
            if in_ordered_list:
                html_lines.append('</ol>')
                in_ordered_list = False
            if in_table:
                html_lines.append('</tbody></table>')
                in_table = False
            html_lines.append('<hr>')
            i += 1
            continue

        # Tables
        if '|' in stripped and stripped.startswith('|') and stripped.endswith('|'):
            if not in_table:
                if in_list:
                    html_lines.append('</ul>')
                    in_list = False
                if in_ordered_list:
                    html_lines.append('</ol>')
                    in_ordered_list = False

                # Parse header
                cells = [c.strip() for c in stripped.split('|')[1:-1]]
                table_headers = cells
                html_lines.append('<table>')
                html_lines.append('<thead><tr>')
                for cell in cells:
                    html_lines.append(f'<th>{parse_inline(cell)}</th>')
                html_lines.append('</tr></thead>')
                html_lines.append('<tbody>')
                in_table = True
                i += 1
                # Skip separator line if present
                if i < len(lines) and '|' in lines[i] and re.match(r'^\|[\s\-:|]+\|$', lines[i].strip()):
                    i += 1
                continue
            else:
                # Table row
                if re.match(r'^\|[\s\-:|]+\|$', stripped):
                    # Separator line, skip
                    i += 1
                    continue
                cells = [c.strip() for c in stripped.split('|')[1:-1]]
                html_lines.append('<tr>')
                for cell in cells:
                    html_lines.append(f'<td>{parse_inline(cell)}</td>')
                html_lines.append('</tr>')
                i += 1
                continue
        else:
            if in_table:
                html_lines.append('</tbody></table>')
                in_table = False

        # Unordered lists
        list_match = re.match(r'^[\s]*[-*+]\s+(.+)$', line)
        if list_match:
            if in_ordered_list:
                html_lines.append('</ol>')
                in_ordered_list = False
            if not in_list:
                html_lines.append('<ul>')
                in_list = True
            content = parse_inline(list_match.group(1))
            html_lines.append(f'<li>{content}</li>')
            i += 1
            continue

        # Ordered lists
        ordered_match = re.match(r'^[\s]*\d+\.\s+(.+)$', line)
        if ordered_match:
            if in_list:
                html_lines.append('</ul>')
                in_list = False
            if not in_ordered_list:
                html_lines.append('<ol>')
                in_ordered_list = True
            content = parse_inline(ordered_match.group(1))
            html_lines.append(f'<li>{content}</li>')
            i += 1
            continue

        # Close lists if we hit something else
        if stripped and not list_match and not ordered_match:
            if in_list:
                html_lines.append('</ul>')
                in_list = False
            if in_ordered_list:
                html_lines.append('</ol>')
                in_ordered_list = False

        # Blockquotes
        if stripped.startswith('>'):
            content = re.sub(r'^>\s*', '', stripped)
            html_lines.append(f'<blockquote>{parse_inline(content)}</blockquote>')
            i += 1
            continue

        # Images (including SVGs) - convert to img tag
        img_match = re.match(r'!\[([^\]]*)\]\(([^\)]+)\)', stripped)
        if img_match:
            alt = img_match.group(1)
            src = img_match.group(2)
            html_lines.append(f'<img src="{src}" alt="{html.escape(alt)}" />')
            i += 1
            continue

        # Empty line
        if not stripped:
            i += 1
            continue

        # Regular paragraph
        if stripped:
            html_lines.append(f'<p>{parse_inline(stripped)}</p>')

        i += 1

    # Close any open tags
    if in_code_block:
        html_lines.append('</code></pre>')
    if in_list:
        html_lines.append('</ul>')
    if in_ordered_list:
        html_lines.append('</ol>')
    if in_table:
        html_lines.append('</tbody></table>')

    return '\n'.join(html_lines)


def convert_markdown_files(input_files, output_file):
    """
    Convert multiple Markdown files to a single self-contained HTML file.
    """
    html_parts = []

    for input_file in input_files:
        input_path = Path(input_file)
        if not input_path.exists():
            print(f"[!] Error: Input file not found: {input_file}", file=sys.stderr)
            sys.exit(1)

        print(f"[+] Processing: {input_file}")

        with open(input_path, 'r', encoding='utf-8') as f:
            markdown_content = f.read()

        # Convert Markdown to HTML
        html_body = convert_markdown_to_html(markdown_content)

        # Inline SVG images (resolve paths relative to the input file's directory)
        base_dir = input_path.parent.absolute()
        html_body = inline_svg_images(html_body, base_dir)

        html_parts.append(html_body)

    # Generate the full HTML document
    full_html = generate_full_html(html_parts)

    # Write output
    output_path = Path(output_file)
    output_path.parent.mkdir(parents=True, exist_ok=True)

    with open(output_path, 'w', encoding='utf-8') as f:
        f.write(full_html)

    print(f"[+] Successfully generated: {output_file}")

    # Report inline SVG count
    svg_count = full_html.count('<svg')
    print(f"[+] Inlined {svg_count} SVG image(s)")


def generate_full_html(html_parts):
    """
    Wrap HTML body parts in a complete HTML document with styling.
    """
    # Concatenate all HTML parts
    combined_body = "\n\n".join(html_parts)

    html_template = f"""<!DOCTYPE html>
<html lang="en">
<head>
    <meta charset="utf-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>AI Infrastructure Guide — GKE + H100 + Inference + Notebooks</title>
    <style>
        body {{
            font-family: 'Arial', 'Helvetica', sans-serif;
            font-size: 11pt;
            line-height: 1.6;
            color: #202124;
            max-width: 8.5in;
            margin: 1in auto;
            padding: 0 1rem;
            background-color: #ffffff;
        }}

        h1 {{
            color: #1a73e8;
            font-size: 20pt;
            border-bottom: 2px solid #e0e0e0;
            padding-bottom: 8px;
            margin-top: 24px;
            margin-bottom: 16px;
            font-weight: bold;
        }}

        h2 {{
            color: #1a73e8;
            font-size: 15pt;
            margin-top: 22px;
            margin-bottom: 10px;
            border-bottom: 1px solid #eeeeee;
            padding-bottom: 4px;
            font-weight: bold;
        }}

        h3 {{
            color: #202124;
            font-size: 13pt;
            margin-top: 18px;
            margin-bottom: 8px;
            font-weight: bold;
        }}

        h4 {{
            color: #5f6368;
            font-size: 11.5pt;
            margin-top: 14px;
            margin-bottom: 6px;
            font-weight: bold;
        }}

        h5 {{
            color: #5f6368;
            font-size: 10.5pt;
            margin-top: 12px;
            margin-bottom: 6px;
            font-weight: bold;
        }}

        h6 {{
            color: #5f6368;
            font-size: 10pt;
            margin-top: 10px;
            margin-bottom: 6px;
            font-weight: bold;
        }}

        p {{
            margin-bottom: 10px;
        }}

        pre {{
            background-color: #f8f9fa;
            border: 1px solid #dadce0;
            border-radius: 6px;
            padding: 12px;
            font-family: 'Courier New', 'Consolas', monospace;
            font-size: 9.5pt;
            white-space: pre-wrap;
            word-wrap: break-word;
            margin-bottom: 14px;
            overflow-x: auto;
        }}

        code {{
            background-color: #f1f3f4;
            color: #d93025;
            font-family: 'Courier New', 'Consolas', monospace;
            font-size: 9.5pt;
            padding: 2px 4px;
            border-radius: 4px;
        }}

        pre code {{
            background-color: transparent;
            color: #202124;
            padding: 0;
        }}

        table {{
            width: 100%;
            border-collapse: collapse;
            margin-bottom: 18px;
            font-size: 10pt;
        }}

        th {{
            background-color: #e8f0fe;
            color: #1a73e8;
            border: 1px solid #dadce0;
            padding: 10px;
            text-align: left;
            font-weight: bold;
        }}

        td {{
            border: 1px solid #dadce0;
            padding: 9px;
            vertical-align: top;
        }}

        ul, ol {{
            margin-top: 4px;
            margin-bottom: 12px;
            padding-left: 24px;
        }}

        li {{
            margin-bottom: 6px;
        }}

        a {{
            color: #1a73e8;
            text-decoration: none;
        }}

        a:hover {{
            text-decoration: underline;
        }}

        blockquote {{
            border-left: 4px solid #dadce0;
            padding-left: 12px;
            color: #5f6368;
            margin: 12px 0;
        }}

        hr {{
            border: 0;
            border-top: 1px solid #dadce0;
            margin: 22px 0;
        }}

        /* SVG styling for embedded diagrams */
        svg {{
            max-width: 100%;
            height: auto;
            display: block;
            margin: 16px auto;
        }}

        /* Alert/callout boxes */
        .admonition {{
            border-left: 4px solid #ea4335;
            background-color: #fce8e6;
            padding: 12px;
            margin: 14px 0;
            border-radius: 0 4px 4px 0;
        }}
    </style>
</head>
<body>
{combined_body}
</body>
</html>"""

    return html_template


def main():
    parser = argparse.ArgumentParser(
        description='Convert Markdown files to self-contained Google-Docs-ready HTML.',
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  %(prog)s guide.md -o output.html
  %(prog)s doc1.md doc2.md doc3.md -o combined.html
        """
    )

    parser.add_argument(
        'inputs',
        nargs='*',
        help='Input Markdown file(s)'
    )

    parser.add_argument(
        '-o', '--output',
        required=False,
        help='Output HTML file path'
    )

    args = parser.parse_args()

    # Validate arguments
    if not args.inputs:
        parser.print_help()
        print("\n[!] Error: No input files specified.", file=sys.stderr)
        sys.exit(1)

    if not args.output:
        parser.print_help()
        print("\n[!] Error: Output file (-o) is required.", file=sys.stderr)
        sys.exit(1)

    # Convert files
    convert_markdown_files(args.inputs, args.output)


if __name__ == "__main__":
    main()
