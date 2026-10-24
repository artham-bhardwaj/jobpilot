# Tailored resumes

JobPilot makes a job-specific PDF of your resume for every eligible job, locally, from the **LaTeX source of your
approved Master resume**. Tailoring only **re-orders** approved, confirmed content. Nothing is written by a model, and
nothing is invented, reworded or added.

## Architecture

`services/tailored.py`, table `tailored_resumes` (migration 0012), CLI `jobpilot resume …` and the **Resume for this
job** panel on a job's page (**Create resume for this job**). Resumes are made **only when you ask**, for any job you
choose (a low score, a short description or an ineligible job only adds a note). Nothing generates them in the
background, and no Telegram message is sent about them (the scheduled `tailor_resumes` task and its notifications were
removed on 2026-10-23).

1. **Template.** `jobpilot resume template --file resume.tex` registers the LaTeX source of the approved Master
   resume version.
   - It is accepted only if its text is the approved version's text: the same letters and digits, nothing more or
     less.
   - It is copied read-only to `data/files/templates/master_v<N>_<hash>.tex`. Its checksum and source version are
     kept in the `tailored_resumes` setting.
   - The original file, the Master resume's record and its PDF are never changed.
   - If a new Master version is approved, the template has to be registered again. Until then nothing is generated
     and nothing is attached.
2. **Parsing.** The source is read into its sections (`\resheading{…}`) and nested `itemize` lists. The parse
   reproduces the file **byte for byte**; anything it can't read losslessly is refused.
3. **Fact mapping.** Every line is mapped to a confirmed profile fact.
   - **Experience entry:** must be a confirmed employment record with the same employer, title and dates (e.g.
     `Dec 2025 – Present` for a start of 2025-12-26 and no end). Any mismatch stops generation.
   - **Experience bullet:** must equal a line of that employment's confirmed description.
   - **Project:** must be a confirmed project. Its bullets must equal lines of its confirmed description.
   - **Skills:** each value maps to a confirmed skill, or is recorded as the approved resume's own wording.
   - **Certifications, achievements:** mapped to their records; differences are reported as warnings.
   - **Education:** mapped to the confirmed record. The years in the entry's date (`\textit{2021}` or
     `\textit{2021 -- 2025}`) are taken from that record: a single year is the completion year; a range is the
     start and end years. A correction is recorded under *corrections* and allowed by validation (exactly that
     text, nothing else). Unconfirmed records never change anything; an entry without confirmed dates is copied
     unchanged, with a note. Example: the approved Master resume shows Class XII 2021; your confirmed record says
     2020, so tailored resumes show 2020. The Master PDF and its source are not changed.
   - **Left out:** anything that maps only to an unconfirmed fact, or to no fact at all, is left out and listed.
4. **Tailoring.** Lines are ordered by the job's skills: required × 3, preferred × 1.5, mentioned × 1 (the job's
   extracted skills).
   - Experience bullets, projects, the bullets inside each project, and the values inside each skills line are
     re-ordered. Ties keep the original order.
   - Wording, metrics, employers, titles, dates and the integration-testing / development distinction are
     copied verbatim (only education years follow your confirmed records, see above).
   - Job skills you don't have are listed under *not claimed* and never added.
   - The template has no summary section, so none is added (the approved structure is kept).
5. **Compiling.**
   - Command: `latexmk -pdf` (or `pdflatex`), run with `-no-shell-escape -halt-on-error -interaction=nonstopmode`.
   - Environment: a temporary build directory and a minimal environment (`openout_any=p`, `openin_any=p`).
   - Limits: a timeout (`compile_timeout_s`, default 90) and no network.
   - Fixed `SOURCE_DATE_EPOCH`, so the same source gives the same PDF bytes.
   - Only the PDF is kept; build files are deleted with the temporary directory.
6. **Validation.** Compiling is not enough. Every check below must pass:
   - `file_exists`: the file exists and is not empty.
   - `parsable`, `text_extractable`.
   - `sections_present` and `candidate_details`: name, email, phone.
   - `all_content_present`: every expected line is in the text, so nothing was clipped or lost.
   - `same_content_as_master`: the same letters and digits as the approved Master resume, minus whatever was left
     out. Nothing was added.
   - `no_placeholders`: `{{ }}`, `TODO`, `??`, leftover `\commands`.
   - `no_latex_errors`.
   - `no_blank_pages`.
   - `page_count` ≤ the target (`page_target`; default: the Master resume's page count).
   - `inside_page`: every word's box is inside the page (`pdftotext -bbox`).
   - `master_unchanged`: the Master file still matches its approved checksum.
7. **Storage.**
   - Path: `data/files/tailored_resumes/<company>_<job id>_<content hash>.pdf`. A different file is never
     overwritten; a numbered name is used instead.
   - The record keeps the job, the company and title at generation time, the posting URL, and the source version.
   - It also keeps hashes of the template, the confirmed facts, the job and the inputs, plus the PDF hash.
   - Statuses (tailoring / compile / validation), the decisions and fact mapping, all checks, any error, and the
     attachment are recorded too.
   - **Reuse:** the same job with unchanged inputs and an intact file reuses the existing PDF. Changed inputs make a
     new record, and the old one becomes `superseded`. Inputs include a pipeline version, so a change in the
     generation rules makes earlier PDFs stale: they are never attached and the next run regenerates them.

## Which jobs

The same rules as everywhere else:
- the job is active and not dismissed;
- its relevance is at or above your threshold;
- it is not ineligible and has no hard conflict;
- it has a usable description (`min_description_chars`, default 400);
- you haven't already applied to it, and no possible duplicate application exists.

Discovery never depends on generation. Each job is generated on its own, so a failure is recorded and the others
continue. A failed job is retried after 24 h.

## Attaching

`tailored.attachment_for` is used by assisted filling (`jobpilot assist`). It uploads the
job's tailored resume only when:
- the tailored resume belongs to that job and company and is `valid` with passed validation;
- its file exists with the recorded hash;
- it was made from the resume the application uses, and the template is unchanged;
- the job and your confirmed facts haven't changed since.

Otherwise the application's approved resume is uploaded (itself checksum-verified), and the reason is logged. You
always review the filled form and click Submit yourself.

Attaching proves nothing about submission. All platform rules stay as they were:
- CAPTCHA, login, MFA, anti-bot checks, legal acknowledgements, consents and signatures are always yours;
- the daily limit and the batch approval are unchanged;
- uncertain submissions are never retried;
- generating a resume never starts an application.

When a form needs you (e.g. a bot check), the Telegram exception says so, with the application link. It also says
when the tailored resume is ready to be attached by the assist run.

## A new Master version

When you approve a new Master resume version (e.g. v2 with a correction):

1. Keep its LaTeX source (e.g. `data/files/resume_sources/<name>.tex`), compile it, and import the PDF as a new version
   of the Master resume (Resumes → upload, or `resumes.import_version`); review and approve it (the review asks for an
   acknowledgement when the comparison with the previous version flags changes).
2. Register its source: `jobpilot resume template --file <source.tex>`. The previous version's source is refused from
   then on (its text no longer matches the approved Master).
3. Tailored resumes made from the previous version become `outdated`: never attached, files and records kept. Create a new
   one on the job's page when you need it.
4. Applications keep the resume version they were prepared or approved with . New applications use the new version. To move an open, not-yet-approved application
   to the new version, choose it on the application page.

Earlier versions stay approved and checksum-protected; nothing is deleted or overwritten.

## Commands

| Command | What it does |
|---|---|
| `jobpilot resume template --file PATH` | register the approved Master resume's LaTeX source |
| `jobpilot resume preview --job ID` | eligibility and every tailoring decision; nothing saved |
| `jobpilot resume generate --job ID` | generate (or reuse) and validate |
| `jobpilot resume regenerate --job ID` | generate again even if unchanged |
| `jobpilot resume validate --job ID` | check the stored PDF again (file, current content, every rule) |
| `jobpilot resume list` / `show --job ID` | records / one record with decisions and checks |
| `jobpilot resume path --job ID` | the validated PDF's path |

Exit codes:
- 0: done
- 1: bad input
- 2: the job is not eligible
- 3: generation or validation failed
- 4: no LaTeX compiler

A database backup is taken before the first generation for a job and before each regeneration.

## Settings and scheduler

The `tailored_resumes` setting holds `enabled`, `attach`, `page_target`, `compile_timeout_s`, `min_description_chars`
(below it, the page notes that the order follows the job title only) and `template`. A `max_per_run` value saved
before 2026-10-23 is ignored.

## Dependencies

`sudo apt install latexmk texlive-latex-recommended texlive-fonts-recommended`. `pdftotext`/`pdfinfo` (poppler-utils)
are used for validation. No Overleaf, no paid API, no upload.

## Recovery and limitations

- **Missing compiler:** the generation is recorded as failed (`compile_status: unavailable`) with the install
  command, and the approved resume is used.
- **Failed validation:** the PDF is kept for diagnosis but never attached. Use `jobpilot resume show --job ID`.
- **Template mismatch or new Master version:** register the new source.
- **Facts changed:** the next run regenerates. Applications use the approved resume until a current tailored one
  exists.
- **No rewording:** improving the wording of a bullet needs your approval. JobPilot only re-orders. To change wording,
  edit your resume, approve the new version, and register its source.
- **No summary:** the template has no summary section, so tailored resumes don't get one.
- **Fit:** content is never dropped to fit a page. If a resume can't meet the page target, validation fails and says
  so.
