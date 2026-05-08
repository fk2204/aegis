# Tier 1 prescribed-form templates

This directory holds **regulator-prescribed disclosure forms** for each
Tier 1 state. Each file in this directory is a Jinja template (`.html.j2`)
that must match the state regulator's published form **line by line**.

## Hard rules

1. **No "best guess" templates.** A Tier 1 template only lands here once
   the operator has provided the regulator's official prescribed form
   (CA DFPI, NY DFS, etc.). No drafts. No paraphrases.
2. **Exact match to the published form.** Headings, ordering, section
   numbering, font emphasis (bold/italic), boilerplate language — all
   preserved as-published. The state regulator audits these; AEGIS does
   not get to "improve" them.
3. **Variable interpolation only.** The only Jinja expressions in a
   template should be `{{ var }}` substitutions for runtime values
   (principal, factor, APR, dates, merchant identity). No `{% if %}`
   logic that changes the disclosure's text — every state's form is a
   fixed shape; the only variability is the numbers filled in.
4. **HTML autoescape is on.** Inputs (merchant name, owner name) cannot
   inject HTML. If a template needs literal markup (a state-mandated
   bold phrase, etc.) it goes in the template file, never in the data.
5. **Filename = `{state_abbr}_{bill_short}.html.j2`.** e.g.
   `ca_sb1235.html.j2`, `ny_cfdl.html.j2`. The `template_path` field on
   the Tier 1 entry in `states.py` references this filename.
6. **Snapshot test required.** Each template gets a snapshot test that
   pins its rendered output for fixed inputs. Updating a snapshot means
   the regulator's form changed and we updated to match — explain why
   in the commit message.

## Boot guard

`compliance/states.py::validate_states_table()` runs at app startup and
rejects any Tier 1 entry whose `template_path` points at a file that
does not exist here. So a Tier 1 entry without a template = the app
refuses to boot. Fail-closed is the only safe behavior.

## What lives here today

Nothing. Phase 4's skeleton ships with all 45 served states in Tier 3
(no audits completed). Templates appear in this directory only when the
operator promotes a state to Tier 1 with regulator-supplied source.

The `_generic_templates.py` module (sibling, in `compliance/`) holds a
generic acknowledgment used for Tier 2 states — those are NOT
regulator-prescribed and intentionally live outside this directory so a
"Tier 1 prescribed form missing" check is unambiguous.
