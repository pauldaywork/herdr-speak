# Public Release Audit Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Confirm that everything that becomes visible when `pauldaywork/herdr-speak` goes public (every commit on GitHub, plus the GitHub-side extras) holds no secrets or private data, fix what doesn't pass, and report back so the user can decide whether to flip the repo to public.

**Architecture:** This is mostly an audit. Established secret scanners (gitleaks and TruffleHog, run from their official Docker images) check the full history of a fresh clone of the GitHub repo. A grep pass and a read-through then catch personal data the scanners don't look for. A code review covers anything that would be unsafe for a stranger to run. GitHub-side extras (issues, Actions secrets, releases, wiki) get checked with `gh`. Fixes are only small file edits and a LICENSE. History rewrites and the visibility change are user decisions that happen only after the user says yes.

**Tech Stack:** git, Docker, `ghcr.io/gitleaks/gitleaks`, `trufflesecurity/trufflehog`, `gh` CLI, grep.

**Spec:** Taskwarrior task `bf0df261-833e-473a-848d-3d6febbdb810` ("can you check to make sure everything in this project is safe to make public. I want to make this repo public but not sure if I have included anything unsafe before doing so"). Read it with `task rc.json.array=on bf0df261-833e-473a-848d-3d6febbdb810 export`.

## Global Constraints

- Audit **what GitHub would publish**: every commit reachable from GitHub refs, not just the working tree. The pre-planning survey found `origin` has only `refs/heads/main` (45 commits, same tip as local `main`, `779b5bc`) and no PRs. Local unreachable objects are never pushed, so they're out of scope.
- Do **not** run `gh repo edit --visibility public`, and do not rewrite or force-push history, without the user's explicit yes in this session. Both are hard to reverse and visible to the outside world.
- Use existing scanners. Don't write a custom secret scanner.
- Scratch output (clones, scan reports) goes in the session scratchpad, never in the repo. Don't commit scan reports. A report in a public repo serves no purpose.
- Licence: the user chose **MIT**, copyright line `Copyright (c) 2026 Paul Day`. A dependency check found nothing that prevents it: only the Python standard library is imported, and everything else (herdr, FFmpeg, mpv, fzf, Neovim, sptlrx, mpv-mpris, Kokoro, `claude`) is run as a separate program, not shipped.
- Credit privateer-speak: `strip_markdown` in `speak.py` follows privateer-speak's `stripMarkdown` (`src/distill.ts`, MIT, Copyright (c) 2026 Patrick (zahnno), https://pi.dev/packages/privateer-speak). The user asked for a credit line.

## What the pre-planning survey already found

Use these as leads to confirm, not as conclusions:

- Files ever tracked: `.gitignore`, `README.md`, `herdr-plugin.toml`, `prompt.md` (deleted in `779b5bc`), `prompt-plan.md`, `speak.py`, `tests/__init__.py`, `tests/test_speak.py`, and three plans under `docs/superpowers/plans/`.
- A keyword grep of `git log --all -p` found no keys, tokens, passwords, private keys, or non-localhost IPs. URLs are public project sites and `127.0.0.1`.
- Personal data in history:
  - The commit author and committer email `work@paulday.com.au` on all 45 commits.
  - `~/Projects/herdr-speak` in a plan's text.
  - Taskwarrior UUIDs such as `1eacd7da-9320-4184-95eb-dcb483a6158e` and mentions of the user's worktree workflow in `docs/superpowers/plans/*.md`.
- No `LICENSE` file. Without one, a public repo is "all rights reserved".
- `speak.py` runs `claude -p` with `--tools ""`, `--strict-mcp-config` and `--setting-sources ""`. It starts Kokoro in Docker bound to `127.0.0.1` only, and passes fzf arguments as argv with no `shell=True`.

---

### Task 1: Scan the full published history with gitleaks and TruffleHog

**Files:**
- No repo files change. Output goes to `$SCRATCH/scan/` (set `SCRATCH` to the session scratchpad directory).

**Interfaces:**
- Produces: `$SCRATCH/scan/gitleaks.json`, `$SCRATCH/scan/trufflehog.json`, and the clone at `$SCRATCH/scan/repo`, which Tasks 2 and 3 use.

- [ ] **Step 1: Clone exactly what GitHub has**

The worktree's `.git` is a file that points outside the worktree, so it can't be mounted into a container. A fresh clone also limits the scan to what GitHub would publish.

```bash
SCRATCH=<session scratchpad>
mkdir -p "$SCRATCH/scan"
git clone --no-local git@github.com:pauldaywork/herdr-speak.git "$SCRATCH/scan/repo"
git -C "$SCRATCH/scan/repo" ls-remote origin
git -C "$SCRATCH/scan/repo" rev-list --count --all
```

Expected: `ls-remote` lists only `HEAD` and `refs/heads/main`, and the count is `45` or more if the user pushed since. If other refs appear (branches, tags, `refs/pull/*`), fetch them all with `git -C "$SCRATCH/scan/repo" fetch origin '+refs/*:refs/remotes/all/*'` so the scan covers them.

- [ ] **Step 2: Run gitleaks over every commit**

```bash
docker run --rm -v "$SCRATCH/scan:/scan" ghcr.io/gitleaks/gitleaks:latest \
  git /scan/repo --log-opts="--all" --redact --verbose \
  --report-format json --report-path /scan/gitleaks.json
echo "exit=$?"
```

Expected: `no leaks found` and `exit=0`. gitleaks exits `1` when it finds leaks. If it does, open `gitleaks.json` and, for each finding, note the commit, file, line and rule, then decide whether it's real or a false positive by reading that line with `git -C "$SCRATCH/scan/repo" show <commit>:<file>`.

- [ ] **Step 3: Run TruffleHog over every commit**

```bash
docker run --rm -v "$SCRATCH/scan:/scan" trufflesecurity/trufflehog:latest \
  git file:///scan/repo --results=verified,unverified,unknown --json \
  > "$SCRATCH/scan/trufflehog.json"
wc -l "$SCRATCH/scan/trufflehog.json"
```

Expected: `0` lines, meaning no findings. TruffleHog's status lines go to stderr. Triage any finding the same way as in Step 2. A `verified` finding is a live credential: tell the user straight away to rotate it, before anything else.

- [ ] **Step 4: Record the results**

Write `$SCRATCH/scan/summary.md` with each tool's version (`docker run --rm ghcr.io/gitleaks/gitleaks:latest version`, `docker run --rm trufflesecurity/trufflehog:latest --version`), the commit count scanned, and each finding with its verdict. No commit, since nothing in the repo changed.

### Task 2: Check history for personal and private data the scanners don't cover

Secret scanners don't flag emails, home paths, hostnames, or private notes. This task reads for those.

**Files:**
- Reads: the clone at `$SCRATCH/scan/repo` from Task 1.
- Output: appended to `$SCRATCH/scan/summary.md`.

**Interfaces:**
- Consumes: `$SCRATCH/scan/repo`.
- Produces: a list of personal-data findings for the user, with the exact files and commits. Task 5 presents it.

- [ ] **Step 1: List every identity in commit metadata**

```bash
git -C "$SCRATCH/scan/repo" log --all --format='%an <%ae>%n%cn <%ce>' | sort | uniq -c
git -C "$SCRATCH/scan/repo" log --all --format='%B' | grep -iE 'co-authored|signed-off|@' | sort | uniq -c
```

Expected, from the survey: only `Paul Day <work@paulday.com.au>`, plus `Co-Authored-By: Claude … <noreply@anthropic.com>` trailers. Record whether the email is one the user is happy to publish. Don't change it. That needs a history rewrite, which is the user's call in Task 5.

- [ ] **Step 2: Grep every version of every file for personal data patterns**

```bash
cd "$SCRATCH/scan/repo"
git log --all -p --format='COMMIT %h' | grep -nEi \
  '/home/|~/(Projects|\.worktrees)|paul|@[a-z0-9.-]+\.[a-z]{2,}|[0-9]{1,3}(\.[0-9]{1,3}){3}|[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}|\.local\b|tailscale|ts\.net|192\.168|10\.[0-9]+\.|ssh |BEGIN |password|passwd|secret|token|api[_-]?key' \
  | grep -v '127\.0\.0\.1' | cut -c1-200 > "$SCRATCH/scan/personal-grep.txt"
wc -l "$SCRATCH/scan/personal-grep.txt"
```

Read every line of `personal-grep.txt`. Classify each as harmless (for example `MAX_THINKING_TOKENS`, `SENTENCE_TOKEN`, `noreply@anthropic.com`, or the `Author:` lines already covered in Step 1) or worth telling the user about. Expected leads from the survey: `~/Projects/herdr-speak`, Taskwarrior UUIDs, and the author email.

- [ ] **Step 3: Read the files most likely to hold private context**

Read these in full at `HEAD`, and read the deleted `prompt.md` with `git show 779b5bc^:prompt.md`:

- `docs/superpowers/plans/2026-09-30-speak-plan-picker.md`
- `docs/superpowers/plans/2026-09-30-speak-selection-captions.md`
- `docs/superpowers/plans/2026-10-01-speak-last-answer-paragraphs.md`
- `prompt.md` (deleted) and `prompt-plan.md`
- `README.md`, `herdr-plugin.toml`

Look for anything about the user's machine, other private projects, employers, clients, or task notes copied in from Taskwarrior. Also check the plans' text in older commits: `git log --all --format=%h -- docs/ | xargs -I{} git show {} --stat` lists the commits, and each one's diff is worth a skim. Note each finding with file and commit.

- [ ] **Step 4: Check for binary or large files**

```bash
git -C "$SCRATCH/scan/repo" rev-list --objects --all \
  | git -C "$SCRATCH/scan/repo" cat-file --batch-check='%(objecttype) %(objectsize) %(rest)' \
  | awk '$1=="blob"' | sort -k2 -n | tail -5
```

Expected: the largest blob is a version of `speak.py`, around 50 KB. No audio, `.lrc`, `.state/` or `__pycache__` files. If any show up, add them to the findings.

### Task 3: Review the code for anything unsafe for a stranger to run

**Files:**
- Reads: `speak.py`, `herdr-plugin.toml`, `README.md` at `HEAD`.
- Output: appended to `$SCRATCH/scan/summary.md`.

**Interfaces:**
- Produces: a list of code-safety findings. Each is either "fine as is", with the reason, or a concrete fix for Task 4.

- [ ] **Step 1: Check every subprocess call and shell string**

```bash
grep -nE 'subprocess|shell=True|os\.system|eval\(|exec\(|sh -c|--preview|--bind|execute' speak.py
```

For each hit, confirm that the arguments are an argv list and that no user-controlled text (pane contents, file names, the selection) reaches a shell. Pay attention to these:
- The fzf `--preview "head -n 200 {1}"` at about `speak.py:941`. fzf quotes `{1}` itself, so confirm a file name such as `a'; touch /tmp/x; '.md` can't break out: create it in a throwaway dir and run the picker command by hand.
- The `--with-shell "sh -c"` Enter binding at about `speak.py:1276`. It should only read `$FZF_SELECT_COUNT`.

- [ ] **Step 2: Check the network and Docker defaults**

Confirm in `speak.py` and `README.md` that:
- The default `baseUrl` is `http://127.0.0.1:8880/v1`.
- Auto-starting Kokoro only happens for `127.0.0.1` or `localhost` (find it with `grep -n 'autoStart\|hostname' speak.py`).
- The `docker run` it builds publishes the port as `127.0.0.1:<port>:8880`, not `0.0.0.0`.

- [ ] **Step 3: Check the `claude -p` call**

In `rewrite_stream` (about `speak.py:327`), confirm that the flags `--tools ""`, `--strict-mcp-config`, `--setting-sources ""` and `--disable-slash-commands` are present, so a pane's text can't make the rewrite run tools. Note that pane text is sent to Anthropic's API through the user's own Claude Code login. That's expected, but the README should say so (Task 4).

- [ ] **Step 4: Check files the plugin writes**

```bash
grep -nE 'STATE_DIR|spokenDir|mkdir|open\(|write_text|chmod|0o' speak.py
```

Confirm everything is written under `~/.local/state/herdr/plugins/speak/` and that nothing is written into the user's project. Spoken text there may contain private answers, so note whether the default permissions (umask, usually `0755`/`0644`) are fine on a single-user machine. That's a note, not a fix.

### Task 4: Make the agreed small fixes and add a LICENSE

The licence (MIT) and the privateer-speak credit are already decided, so Steps 1, 2, 4 and 5 run now. Step 3 runs only if the user later asks to scrub the current tree.

**Files:**
- Create: `LICENSE`
- Modify: `README.md` (add a "Privacy" note and a "License" section with the privateer-speak credit)
- Modify: `speak.py` (one comment above `strip_markdown` crediting privateer-speak)
- Modify, only if the user asks: `docs/superpowers/plans/*.md`, to remove home paths and Taskwarrior UUIDs from the current tree. That doesn't remove them from history. See Task 5.

**Interfaces:**
- Consumes: the user's answers from Task 5 Step 1.

- [ ] **Step 1: Add the LICENSE the user picked**

For MIT, the GitHub default for small tools:

```bash
gh api licenses/mit --jq .body \
  | sed -e "s/\[year\]/2026/" -e "s/\[fullname\]/Paul Day/" > LICENSE
head -3 LICENSE
```

Expected: `MIT License`, a blank line, and `Copyright (c) 2026 Paul Day`. For another licence, use `gh api licenses/<key>` with the key the user picked (for example `apache-2.0`) and fill in its placeholders.

- [ ] **Step 2: Add a Privacy note and a License section to README.md**

Add these at the end of `README.md`:

```markdown
## Privacy

Text sent to `speak.plan` and `speak.selection` is rewritten by `claude -p`, so it goes to Anthropic through your own Claude Code login. The audio is made by your Kokoro server, which by default runs locally on `127.0.0.1`. Spoken text, captions and audio are kept under `~/.local/state/herdr/plugins/speak/`.

## License

MIT. See [LICENSE](LICENSE).

The markdown stripping in `speak.py` is adapted from [privateer-speak](https://pi.dev/packages/privateer-speak) by Patrick (zahnno), also MIT licensed.
```

Then add this comment on the line directly above `def strip_markdown(text):` in `speak.py`:

```python
# Adapted from privateer-speak's stripMarkdown (src/distill.ts, MIT, (c) 2026 Patrick (zahnno)).
```

Before writing it, check that these claims match the code. Run `grep -n 'rewrite_stream\|claude' speak.py` and check which actions call `rewrite_stream`, then adjust the action names to match. Change "MIT" if the user picked another licence.

- [ ] **Step 3: Scrub the current tree, only if the user asked**

For each finding the user wants removed, replace the private text with something generic. For example, `herdr plugin link ~/Projects/herdr-speak` becomes `herdr plugin link /path/to/herdr-speak`, and `Spec: Taskwarrior task <uuid>…` becomes `Spec: an internal task`. Then check that none remain:

```bash
git grep -nE '~/Projects|/home/|[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}'
```

Expected: no output, or only matches the user chose to keep.

- [ ] **Step 4: Run the tests**

```bash
python3 -m unittest discover -s tests -v 2>&1 | tail -3
```

Expected: `OK`. Only docs and a comment changed, so this guards against an accidental edit.

- [ ] **Step 5: Commit**

```bash
git add LICENSE README.md speak.py
git commit -m "Add an MIT licence, a privacy note, and credit privateer-speak for the markdown stripping

Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>"
```

### Task 5: Report to the user and get their decisions

**Interfaces:**
- Consumes: `$SCRATCH/scan/summary.md` from Tasks 1 to 3.

- [ ] **Step 1: Present the findings and ask for decisions**

Give a short report. Start with the verdict, for example "no secrets in any of the 45 commits". Then list each personal-data and code finding with file and commit. Then ask, with AskUserQuestion:

1. **Current-tree scrub:** which personal-data findings to remove from the current files, if any. This includes keeping or deleting `docs/superpowers/plans/`.
2. **History:** keep history as is (recommended when the only findings are the author email, a home-relative path and task UUIDs, which are low-risk), or rewrite it with `git filter-repo`. A rewrite could swap `work@paulday.com.au` for the GitHub noreply address (`gh api user --jq '"\(.id)+\(.login)@users.noreply.github.com"'`) and strip the plan text. It needs a force-push and makes every existing clone and worktree stale.

If Task 1 found a verified live credential, the history question isn't optional. Tell the user to rotate the credential first. Rotating is the fix. A rewrite only cleans up after it.

- [ ] **Step 2: Carry out Task 4 with the answers**

If the user picked a history rewrite, write a separate plan for it, using `git filter-repo` (https://github.com/newren/git-filter-repo) with `--mailmap` and `--replace-text`. Don't improvise it here.

- [ ] **Step 3: Check the GitHub-side extras that also go public**

```bash
R=pauldaywork/herdr-speak
gh issue list -R $R --state all
gh pr list -R $R --state all
gh release list -R $R
gh api repos/$R/actions/secrets --jq .total_count
gh api repos/$R --jq '{has_wiki, has_discussions, description, homepage}'
```

Expected: no issues, PRs, releases or Actions secrets (the survey found no PRs). Read the description. If any issue or PR exists, read it for private data before going public.

- [ ] **Step 4: Offer the visibility switch, but don't run it unasked**

Once the user has seen the report and the Task 4 commit has landed on `main` and been pushed (use the `finish-worktree` skill), give them the command and run it only on an explicit yes:

```bash
gh repo edit pauldaywork/herdr-speak --visibility public --accept-visibility-change-consequences
gh repo view pauldaywork/herdr-speak --json visibility --jq .visibility
```

Expected after a yes: `PUBLIC`.

- [ ] **Step 5: Close the Taskwarrior task**

When the user is satisfied:

```bash
task bf0df261-833e-473a-848d-3d6febbdb810 annotate "Audit: <one-line verdict>"
task bf0df261-833e-473a-848d-3d6febbdb810 done
```
