"""Regenerate app/templates/plan.html from the ROI page (the artifact source).

The page is authored standalone; here its CSS is scoped under .roi so it cannot fight
base.html, and its <title>/font links/body rules are dropped because base.html owns them.
Usage: python scripts/build_plan.py path/to/klaviyo-exit-roi.html
"""
import re, sys
from pathlib import Path

src = Path(sys.argv[1]).read_text()
css = src[src.index("<style>") + 7: src.index("</style>")]
body = src[src.index('<div class="wrap">'): src.index("<script>")]
js = src[src.index("<script>"): src.index("</script>") + 9]

out = []
for line in css.splitlines():
    t = line.strip()
    if not t or t.startswith("/*") or t.startswith(":root") or t.startswith("}") or t.startswith("--") \
            or t.startswith("@media (prefers-"):
        out.append(line); continue
    if t.startswith("*{") or t.startswith("body{"):
        continue
    if t.startswith("@media (max-width:900px)"):
        out.append("  @media (max-width:900px){.roi .tiers{grid-template-columns:1fr}.roi .top,.roi .floor{grid-template-columns:1fr}.roi .strip{grid-template-columns:repeat(3,1fr)}}"); continue
    out.append(re.sub(r"(^|\}\s*)([^{}@]+)\{", lambda m: m.group(1) + ",".join(".roi " + x.strip() for x in m.group(2).split(",")) + "{", line))
css = "\n".join(out).replace(".roi .wrap{", ".roi .roi-wrap{").replace(".roi .sec{", ".roi .rsec{").replace(".roi .sec .eyebrow", ".roi .rsec .eyebrow")
css = css.replace(".roi h1{margin:0;font-size:28px", ".roi h1{margin:0;font-size:26px")
body = body.replace('<div class="wrap">', '<div class="roi"><div class="roi-wrap">', 1).replace('<div class="sec">', '<div class="rsec">').rstrip() + "\n</div>\n"
js = js.replace('<div class="sec">', '<div class="rsec">')

tpl = f'''{{% extends "base.html" %}}
{{% block title %}}Plan — EMCC{{% endblock %}}
{{% block headright %}}<a class="mono" href="https://claude.ai/artifact/4xKGvkqymeNqFZjXLMp89c" target="_blank" rel="noopener" style="text-decoration:none">standalone ↗</a>{{% endblock %}}
{{% block main %}}
<style>{css}</style>
{body}{js}
{{% endblock %}}
'''
assert "{#" not in tpl
Path(__file__).resolve().parent.parent.joinpath("app/templates/plan.html").write_text(tpl)
print("plan.html rebuilt:", len(tpl), "bytes")
