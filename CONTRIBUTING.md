# Contributing to Crucible

Thank you for considering a contribution. Please read this entire document
**before** opening a pull request — it contains a binding license clause
that applies to every contribution you submit.

---

## Inbound License (binding)

By submitting a contribution to this project — including, but not limited to,
pull requests, patches, code snippets, configuration files, documentation,
test cases, issue templates, or any other material ("**Contribution**") —
**you irrevocably agree** to the following terms:

1. **Dual licensing of your Contribution.** You license your Contribution to
   the project maintainer (Johnson Lai, GitHub: `Starlight143`) and to all
   downstream recipients under **both** of the following:

   a. The **GNU Affero General Public License, version 3.0 or any later
      version** (AGPL-3.0-or-later); **and**

   b. The terms of **any commercial license** that the project maintainer
      offers — now or in the future — under Crucible's dual-license model
      (see [LICENSE](LICENSE) and [COMMERCIAL_LICENSE.md](COMMERCIAL_LICENSE.md)).

2. **Right to relicense.** You grant the project maintainer the perpetual,
   worldwide, royalty-free, irrevocable right to **sublicense, relicense,
   and commercially license** your Contribution as part of Crucible, without
   any further notice to or consent from you. This includes the right to
   include your Contribution in proprietary commercial distributions of
   Crucible sold to third parties.

3. **Originality and authority.** You represent and warrant that:
   - the Contribution is **your original work**, or you have the legal right
     to submit it (e.g. under the original author's license);
   - you have the **legal authority** to license the Contribution under the
     terms above (e.g. it is not subject to a conflicting employment or
     consulting agreement);
   - the Contribution does **not knowingly infringe** any third-party
     intellectual property rights;
   - the Contribution does **not include** code copied from sources that are
     incompatible with AGPL-3.0-or-later (e.g. proprietary, GPL-incompatible,
     or undisclosed-license code).

4. **No obligation to merge.** The project maintainer is under no obligation
   to accept, merge, attribute, or retain any Contribution. Submission does
   not entitle you to compensation, attribution, or any payment from
   commercial licensing revenue.

5. **No retraction.** Once submitted, your Contribution and the license
   grants in sections 1–2 are **irrevocable**. You cannot later withdraw the
   Contribution or restrict how it is licensed.

**If you do not agree to these terms, do not submit Contributions.** Any
pull request, patch, or other Contribution submitted to this repository is
deemed acceptance of these terms in full.

---

## Why this clause exists

Crucible is offered under a dual-license model: AGPL-3.0-or-later for open
source use, and a separate commercial license for organizations that cannot
comply with AGPL-3.0. This model only works if **the project maintainer is
the sole party with the right to grant a commercial license** to all of the
code in the repository.

If contributors retained sole copyright over their submissions and only
licensed them under AGPL-3.0, the project maintainer would have **no legal
authority** to include those submissions in a commercial license sold to a
third party — which would either:
- force the project maintainer to **strip every contribution** before
  selling a commercial license (impractical), or
- silently sell something that **infringes the contributor's copyright**
  (illegal).

The dual inbound license above resolves this by giving the project
maintainer the right to relicense Contributions under both AGPL-3.0 and
future commercial terms, while contributors retain their own copyright
and can also use their own Contribution however they like.

This is the same pattern used by projects such as MongoDB (pre-SSPL),
Sentry, GitLab EE, Aiven, and many others.

---

## How to contribute

### Reporting bugs

Open a [GitHub issue](https://github.com/Starlight143/crucible/issues) with:
- Crucible version (`git rev-parse HEAD` or release tag)
- Python version, OS, and shell
- Minimal reproduction steps
- Expected vs actual behavior
- Relevant logs (redact API keys before pasting)

### Submitting a pull request

1. **Fork** the repository and create a topic branch off `main`.
2. **Make your change.** Keep PRs focused — one logical change per PR.
3. **Run the validation suite** before submitting:
   ```bash
   python -m pytest tests -q -p no:cacheprovider
   python ./crucible/smoke_test.py
   python ./run_crucible.py --self-check
   ```
   All three must pass.
4. **Follow existing code conventions.** Match the surrounding style; do not
   reformat unrelated code.
5. **Write tests** for new behavior. Bug fixes should include a regression
   test that fails without the fix.
6. **Use conventional commit messages** (e.g. `fix:`, `feat:`, `docs:`,
   `refactor:`, `test:`, `chore:`).
7. **Open the PR** against `main`. Describe what changed and why; link
   any related issues.

By opening the PR, you confirm that you have read and accepted the
**Inbound License** above.

### Reporting security issues

**Do not open a public issue for security vulnerabilities.** Email
**supervenus928@gmail.com** with details. You will receive an
acknowledgement and a coordinated disclosure timeline.

---

## Questions about licensing

For commercial license inquiries, see [COMMERCIAL_LICENSE.md](COMMERCIAL_LICENSE.md)
or email **supervenus928@gmail.com**.

For questions about whether your contribution can be accepted under the
inbound license terms above (e.g. you are contributing on behalf of an
employer and need clarification), email the same address before submitting.
