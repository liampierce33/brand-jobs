"""Build index.html from the template, embedding skills.json and the current jobs.json as fallback data."""
import json
from pathlib import Path
root = Path(__file__).resolve().parent
safe = lambda o: json.dumps(o, ensure_ascii=False).replace("</", "<\\/")
html = (root / "dashboard.template.html").read_text()
html = html.replace("/*SEED*/", safe(json.loads((root / "jobs.json").read_text())))
html = html.replace("/*SKILLS*/", safe(json.loads((root / "skills.json").read_text())))
(root / "index.html").write_text(html)
print("index.html built,", len(html) // 1024, "KB")
