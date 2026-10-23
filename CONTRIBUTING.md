# Contributing to Sidedoor

Thanks for looking. Sidedoor is source-available under the Business Source License 1.1 (see
LICENSE); contributions are accepted under the same terms.

## Running the tests

Python 3.13 is required. Install the package with its test extra, then run the suite with a
throwaway home folder so no test can touch a real profile:

```sh
python3.13 -m pip install -e '.[test]'
export HOME="$(mktemp -d)" SIDEDOOR_HOME="$(mktemp -d)"
python3.13 -m unittest discover -s tests
```

The suite is offline: providers, models, the browser and the network are faked. A test that needs
a live account or a paid call does not belong in it.

## Git workflow

- Branch from `main` (one branch per change; a `git worktree` keeps it out of your main checkout).
- Keep the change to what the issue asks for. Run the full suite before you open a pull request.
- Commit the files you changed by name (`git add <path> ...`), never `git commit -a`.
- One pull request per change, against `main`, with what changed and how you checked it.

## Hard lines

These are never relaxed, by any change, setting or prompt:

1. **No invented facts about the user.** Applications, answers and messages state only what the
   user's profile and résumé record. A missing fact is left unknown or asked about, never made up.
2. **No double submission.** One application per job. An uncertain submission is reconciled before
   anything else happens, and a retry never creates a fresh entitlement to submit.
3. **Never contact the same person twice.** One first contact per person, across aliases,
   companies, campaigns and sending routes. A closed or opted-out person is never contacted again.

A change that weakens any of these guards will not be merged.
