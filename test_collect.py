"""Offline test: fake API responses -> run the pipeline twice -> check filtering, salary, dedupe, new/expired."""
import json, os, shutil, sys, tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
import collect  # noqa: E402

GH = {"meta": {"name": "SimpliSafe"}, "jobs": [
    {"id": 1, "title": "Manager, Brand", "location": {"name": "Boston, MA"}, "absolute_url": "https://job-boards.greenhouse.io/simplisafe/jobs/1",
     "updated_at": "2026-11-07T10:00:00Z", "content": "&lt;p&gt;Hybrid: 2 core office days per week.&lt;/p&gt;&lt;li&gt;5+ years of experience in brand management&lt;/li&gt;&lt;p&gt;Pay: $141,000 - $188,000. Agency budget, creative brief, brand guidelines.&lt;/p&gt;"},
    {"id": 2, "title": "Senior Software Engineer", "location": {"name": "Boston, MA"}, "absolute_url": "x", "content": ""},
    {"id": 3, "title": "Employer Brand Manager", "location": {"name": "Boston, MA"}, "absolute_url": "y", "content": ""},
    {"id": 4, "title": "Brand Marketing Manager", "location": {"name": "Austin, TX"}, "absolute_url": "z", "content": ""}]}
LEVER = [{"id": "a", "text": "Senior Brand Marketing Manager", "categories": {"location": "Somerville, MA"}, "hostedUrl": "https://jobs.lever.co/janji/a",
          "createdAt": 1762300000000, "descriptionPlain": "Own the content calendar. Storytelling.", "lists": [], "additionalPlain": "",
          "workplaceType": "hybrid", "salaryRange": {"min": 95000, "max": 120000, "interval": "per-year-salary"}}]
JSEARCH = {"data": [
    {"job_id": "j1", "employer_name": "SimpliSafe Inc.", "job_title": "Manager, Brand", "job_city": "Boston", "job_state": "MA",
     "job_apply_link": "https://www.linkedin.com/jobs/view/1", "job_description": "brand", "job_posted_at_datetime_utc": "2026-11-06T00:00:00Z",
     "job_publisher": "LinkedIn"},
    {"job_id": "j2", "employer_name": "Acme Foods", "job_title": "Associate Brand Manager", "job_city": "Quincy", "job_state": "MA",
     "job_apply_link": "https://acme.example/jobs/2", "job_description": "On-site role. 3+ years CPG, Nielsen, P&L.",
     "job_min_salary": 40, "job_max_salary": 50, "job_salary_period": "HOUR", "job_publisher": "Indeed"}]}
ADZ = {"results": [{"id": 9, "title": "Brand Strategy Manager", "company": {"display_name": "Somewhere Co"},
                    "location": {"display_name": "Cambridge, Middlesex County"}, "redirect_url": "https://adzuna.example/9",
                    "description": "Hybrid. Brand strategy.", "salary_min": 90000, "salary_max": 90000, "salary_is_predicted": "1"}]}


def fake_get_json(url, **kw):
    if "greenhouse" in url:
        if "simplisafe" in url:
            return GH
        raise RuntimeError("404 board not found")
    if "lever" in url:
        if "janji" in url:
            return LEVER
        raise RuntimeError("404")
    if "ashby" in url:
        raise RuntimeError("404")
    if "jsearch" in url:
        return JSEARCH
    if "adzuna" in url:
        return ADZ
    raise AssertionError(url)


def run():
    tmp = Path(tempfile.mkdtemp())
    for f in ("config.yml", "skills.json"):
        shutil.copy(ROOT / f, tmp / f)
    shutil.copy(ROOT / "jobs.json", tmp / "jobs.json")  # start from the real seed
    collect.JOBS_FILE = tmp / "jobs.json"
    collect.get_json = fake_get_json
    collect.link_alive = lambda url: True
    os.environ.update(JSEARCH_API_KEY="x", ADZUNA_APP_ID="x", ADZUNA_APP_KEY="x")
    for k in ("NTFY_TOPIC", "GMAIL_USER", "ANTHROPIC_API_KEY"):
        os.environ.pop(k, None)

    collect.main([])
    d = json.loads((tmp / "jobs.json").read_text())
    by = {j["id"]: j for j in d["jobs"]}
    titles = [j["title"] for j in d["jobs"]]
    assert "Senior Software Engineer" not in titles and "Employer Brand Manager" not in titles
    assert not any(j["company"] == "Simplisafe" and j["city"] != "Boston" for j in d["jobs"])
    # SimpliSafe from Greenhouse + LinkedIn copy + seed -> one listing, employer copy wins, keeps seed id and contact
    ss = [j for j in d["jobs"] if "simplisafe" in j["company"].lower()]
    assert len(ss) == 1, [(j["id"], j["title"]) for j in ss]
    s = ss[0]
    assert s["id"] == "simplisafe-manager-brand" and s["status"] == "verified" and s["arrangement"] == "Hybrid"
    assert (s["salaryMin"], s["salaryMax"]) == (141000, 188000) and s["contacts"][0]["name"] == "Kaitlin Bonner"
    assert s["firstSeen"] == "2026-11-08"
    acme = [j for j in d["jobs"] if j["company"] == "Acme Foods"][0]
    assert (acme["salaryMin"], acme["salaryMax"]) == (83200, 104000) and acme["arrangement"] == "On-site" and acme["yearsMin"] == 3
    adz = [j for j in d["jobs"] if j["company"] == "Somewhere Co"][0]
    assert adz["salaryMin"] is None, "predicted Adzuna salary must be ignored"
    janji = [j for j in d["jobs"] if j["company"].lower() == "janji"]
    assert len(janji) == 1 and janji[0]["salaryMax"] == 120000
    assert len(d["jobs"]) == 15 + 2  # seed (Janji + SimpliSafe merged) + Acme + Somewhere
    assert set(d["meta"]["newToday"]) == {acme["id"], adz["id"]}, d["meta"]["newToday"]
    print("run 1 ok:", len(d["jobs"]), "jobs; new:", d["meta"]["newToday"])

    # Run 2, three days later: SimpliSafe vanished from its employer board -> expired at once;
    # Acme (aggregator) not seen for 3 days -> expired; seed jobs survive.
    GH["jobs"] = GH["jobs"][1:]
    JSEARCH["data"] = []
    ADZ["results"] = []
    collect.TODAY = "2026-11-11"
    collect.main([])
    d2 = json.loads((tmp / "jobs.json").read_text())
    ids = {j["id"] for j in d2["jobs"]}
    assert "simplisafe-manager-brand" not in ids and acme["id"] not in ids
    assert "wayfair-brand-strategy-manager" in ids
    print("run 2 ok: expired", d2["meta"]["expiredToday"])


if __name__ == "__main__":
    run()
    print("ALL TESTS PASSED")
