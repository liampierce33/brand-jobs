# Brand-role job tracker for Greater Boston

A free, self-updating dashboard of brand marketing roles in Boston, Somerville, Medford, Charlestown, Quincy and Cambridge. It sends alerts when new roles appear.

**How it works:** GitHub runs `collect.py` five times a day for free. The script pulls roles from official job-board APIs, keeps only brand roles in the six cities, removes duplicates and expired postings, and saves `jobs.json`. GitHub Pages serves `index.html`, which reads `jobs.json`. Each new role triggers a phone push (ntfy) and/or an email (Gmail).

Setup takes about 30 minutes and costs $0.

---

## Step 1: Put the files on GitHub (10 min)

1. Sign in at **github.com** (or create a free account).
2. Top-right **+** → **New repository**.
   - Repository name: `brand-jobs`
   - Set it to **Public**. Free Pages hosting needs this. Only job listings are stored here; her resume and email are not.
   - Leave everything else unchecked → **Create repository**.
3. On the empty repo page, click **uploading an existing file**.
4. Unzip `brand-job-tracker.zip` on your computer. Drag **all** the files and folders into the browser window, then **Commit changes**.
   - On a Mac, Finder hides the `.github` folder. Press **Cmd + Shift + .** in Finder to show it, then drag it in too.
   - If it still doesn't upload: click **Add file → Create new file**, type the name `.github/workflows/update-jobs.yml` (the slashes create the folders), paste in the contents of that file from the zip, and **Commit**.
5. **Settings → Pages**. Under "Build and deployment", set Source to **Deploy from a branch**, Branch to **main**, folder **/ (root)**, then **Save**.
   About a minute later the dashboard is live at **`https://YOUR-USERNAME.github.io/brand-jobs/`**. Bookmark it on her phone and laptop.
6. **Settings → Actions → General → Workflow permissions**: choose **Read and write permissions** → **Save**.

## Step 2: Turn on alerts (10 min)

All keys go in **Settings → Secrets and variables → Actions → New repository secret**. Paste the name exactly as shown, then the value.

### Phone push (recommended: instant and free)
1. On her phone, install the **ntfy** app (App Store / Google Play).
2. Tap **+**, then **Subscribe to topic**. Make up a long, unguessable name, e.g. `brandjobs-maple-otter-4821`. Anyone who knows the name can read the alerts, so don't use her name.
3. Add the secret **`NTFY_TOPIC`** = that topic name.

### Email
1. Use a Gmail account to send from (yours, or a new one). Turn on 2-Step Verification at **myaccount.google.com → Security**.
2. Go to **myaccount.google.com/apppasswords**, create an app password named "job tracker", and copy the 16-character code.
3. Add three secrets:
   - **`GMAIL_USER`** = the sending Gmail address
   - **`GMAIL_APP_PASSWORD`** = the 16-character code (no spaces)
   - **`ALERT_EMAIL_TO`** = her email (comma-separate to add yours)

## Step 3: Add job-search sources (10 min)

The employer job boards (Greenhouse, Lever, Ashby) work with no key. These two free keys add coverage of LinkedIn, Indeed, Glassdoor, ZipRecruiter and Built In:

**JSearch** (Google for Jobs index, which includes LinkedIn, Indeed, Glassdoor and Built In listings)
1. Sign up at **rapidapi.com**. Search **"JSearch"** (by OpenWeb Ninja), then **Subscribe to Test** and pick the free Basic plan.
2. Copy the **X-RapidAPI-Key** shown on the endpoint page and add the secret **`JSEARCH_API_KEY`**.
3. The tracker makes 4 calls a day (≈120 a month). Check that this fits the free quota on your plan page.

**Adzuna**
1. Register at **developer.adzuna.com**. It gives you an **Application ID** and an **Application Key**.
2. Add the secrets **`ADZUNA_APP_ID`** and **`ADZUNA_APP_KEY`**.

**Optional:** **`ANTHROPIC_API_KEY`** (from console.anthropic.com) makes the "How to land it" tips for each new role AI-written from the job description, at a fraction of a cent per role. Without it, the tips are rule-based.

## Step 4: Run it

1. Open the **Actions** tab. If asked, click **I understand my workflows, go ahead and enable them**.
2. Click **Update jobs → Run workflow → Run workflow**. In about a minute you should see a green check.
3. Open the dashboard URL. The dot at the top turns green: "Live data, updated …".

From then on it runs automatically at about 6:40am, 9:40am, 12:40pm, 3:40pm and 6:40pm Eastern. Alerts go out only when a role appears that wasn't there before. The 15 roles researched on Nov 8 count as already seen.

---

## Day-to-day

| To… | Do this |
|---|---|
| Track another company directly | Find its careers link (e.g. `job-boards.greenhouse.io/acme`) and add `acme` under `greenhouse:` (or `lever:` / `ashby:`) in `config.yml` (pencil icon on GitHub). Wrong names are skipped harmlessly. |
| Add an office street address | Add it under `addresses:` in `config.yml`, with the company name in lower case. |
| Add a real contact you've confirmed | Add it under `contacts:` in `config.yml`. The script never invents names. |
| Change cities, title keywords or the $80k floor | Edit `cities`, `title_include` / `title_exclude`, or `salary_floor` in `config.yml`. |
| See what happened on a run | Open **Actions**, click the run, then the "Collect jobs" step. A short log is also saved in `jobs.json` under `meta.log`. |

**Expired postings:** a role from an employer job board disappears as soon as the employer removes it. A role found only via JSearch or Adzuna disappears after 3 days unseen. The researched starter roles are dropped when their link dies or after 45 days.

**Housekeeping:** GitHub pauses scheduled jobs in repos with no activity for 60 days. The bot's own commits normally prevent that, but if GitHub ever emails you about it, click the link to re-enable.

## Source rules and reliability

- **Greenhouse, Lever and Ashby** are the public APIs companies use to power their own careers pages. These are the most reliable and are labeled "Verified" in the dashboard.
- **LinkedIn, Indeed and Glassdoor** don't allow scraping in their terms, so this tool doesn't scrape them. It reaches their listings through JSearch's API. Coverage depends on what Google for Jobs indexes, and some links open the job board rather than the employer page. These are labeled "Listed on job boards."
- **Built In Boston** has no public API. Its listings show up through JSearch.
- **Adzuna** sometimes *estimates* salaries. The script drops those, because salaries are only shown when the posting states them.
- Massachusetts' pay-transparency law means most larger employers now post ranges. "Salary not listed" usually means a small company or a job-board summary that cut the range off.

## Files

`index.html` is the dashboard (built from `dashboard.template.html` by `build_page.py`). `collect.py` is the collector. `config.yml` holds settings. `skills.json` is the skill list used for "key skills" and resume fit. `seed_jobs.py` holds the researched starter roles. `tests/test_collect.py` is an offline test (`python tests/test_collect.py`).
