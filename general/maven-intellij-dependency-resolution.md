# Maven "was not found in repo.maven.apache.org" — CLI green, IntelliJ red

Playbook for the Ultron import failure where `./mvnw` builds fine but IntelliJ's Maven
sync reports a handful of artifacts as missing from Maven Central.

> First occurrence: 2026-08-27, branch `ONC-418-nd-fcaps-lc`, IntelliJ 2026.2 (Remote Dev),
> Ultron at `/home/rishi_srivastava/repos/Ultron`.

---

## The Symptom

IntelliJ → *Sync and reload projects* produces, per artifact:

```
com.clevertap:bouncy-castle:jar:1.5.4 was not found in https://repo.maven.apache.org/maven2
during a previous attempt. This failure was cached in the local repository and resolution is
not reattempted until the update interval of central has elapsed or updates are forced

Try to run Maven import with -U flag (force update snapshots)
```

Typically 8 artifacts at once:

| Artifact | Real source |
|---|---|
| `com.clevertap:bouncy-castle:1.5.4` | `project-local` |
| `com.clevertap:clevertap-smack:1.0.0` | `project-local` |
| `gnu.trove:trove:3.0.3` | `project-local` |
| `com.amazon.athena:athena-jdbc:3.2.0` | `project-local` |
| `software.amazon.awssdk:athena-streaming:2.0` | `project-local` |
| `com.simba.googlebigquery:googlebigquery-jdbc42:1.6.3.1004` | `project-local` |
| `com.vowpalwabbit:vw-jni-native-linux-aarch64:9.11.1` | `project-local` |
| `io.confluent:kafka-avro-serializer:5.3.0` | `confluent` |

**Key discriminator:** CLI green + IDE red is *always* a repo-set mismatch. If both were
broken it would be a network or Git-LFS problem instead. The suggested `-U` never fixes this
one — and `./mvnw -U` "succeeding" is not evidence of anything, because the CLI was never
using the broken repo list.

---

## Why It Happens

Ultron does not resolve everything from Central. Two extra repos are declared in
`dashboard/dashboard/m2-settings.xml`:

- **`confluent`** → `https://packages.confluent.io/maven/`
- **`project-local`** → `file:///${project.basedir}/.mvn/legacy_deps` — vendored internal
  jars that exist in Artifactory only as Git-LFS pointers

The CLI gets that file; the IDE does not:

```
./mvnw  ──reads──>  .mvn/maven.config
                    └── "-s /tmp/ultron-m2-settings.xml"
                        (mvnw copies dashboard/dashboard/m2-settings.xml there on every run)
                        => repo list = central + confluent + project-local   ✅

IntelliJ ─reads──>  .idea/workspace.xml  (mavenHomeTypeForPersistence = WRAPPER,
                    no userSettingsFile override)
                    └── falls back to ~/.m2/settings.xml
                        => if that file is absent: repo list = central + pom-declared only  ❌
```

IntelliJ's importer does **not** honour the `-s` in `.mvn/maven.config`, and would not
interpolate `${project.basedir}` inside a settings file even if it did.

### The part that confuses everyone

The jars are already sitting in `~/.m2/repository`. They still fail. Maven Resolver tracks
provenance per artifact in a `_remote.repositories` sibling file:

```
$ cat ~/.m2/repository/com/clevertap/bouncy-castle/1.5.4/_remote.repositories
bouncy-castle-1.5.4.pom>project-local=
bouncy-castle-1.5.4.jar>project-local=
```

A tracked artifact is only served to a build whose **effective repo list contains that
repository id**. IntelliJ's list has no `project-local`, so the local copy is ignored, Central
is tried, it 404s, and the failure is cached in a `.lastUpdated` sibling:

```
$ cat ~/.m2/repository/com/clevertap/bouncy-castle/1.5.4/bouncy-castle-1.5.4.jar.lastUpdated
https\://repo.maven.apache.org/maven2/.error=      <-- empty value = "not found"
```

That cached `.error` is what the message means by "failure was cached". A CLI `-U` cannot
clear it, because on the CLI the artifact resolves from `project-local` and Central is never
consulted.

---

## Triage — three commands, in order

```bash
# 1. Is the jar already on disk?  If yes, this was NEVER a download problem.
ls ~/.m2/repository/com/clevertap/bouncy-castle/1.5.4/

# 2. Which repo id does Maven think owns it?
cat ~/.m2/repository/com/clevertap/bouncy-castle/1.5.4/_remote.repositories

# 3. Does the *consumer's* repo list contain that id?
cat ~/.m2/settings.xml                          # what IntelliJ reads by default
grep -n userSettingsFile .idea/workspace.xml    # unset => it uses the file above
cat .mvn/maven.config                           # what the CLI reads
```

Jar present (1) + id from (2) absent from (3) = confirmed. Stop looking at the network.

---

## The Fix

Order matters. Clearing markers before fixing settings just re-caches the same failures on
the next import.

### 1. Give IntelliJ the right repo list

Regenerate `~/.m2/settings.xml` from the tracked template, substituting the absolute path
(run from the Ultron repo root):

```bash
sed "s|file:///\${project.basedir}|file://$PWD|" \
    dashboard/dashboard/m2-settings.xml > ~/.m2/settings.xml
```

`~/.m2/settings.xml` is what IntelliJ reads with no override configured, so this needs zero
UI clicks. To be explicit instead: *Settings → Build, Execution, Deployment → Build Tools →
Maven → User settings file → Override →* `/home/<user>/.m2/settings.xml`.

Do **not** point the IDE at `/tmp/ultron-m2-settings.xml`: it is wiped on reboot and it keeps
`${project.basedir}` unexpanded.

### 2. Clear the cached failures

```bash
find ~/.m2/repository -name "*.lastUpdated" -delete
```

Safe and reversible — these are failure markers only. Jars and poms are untouched.

### 3. Reload

IntelliJ → Maven tool window → **Reload All Maven Projects**.

---

## Verify Without the IDE

Run from **outside** the repo so `.mvn/maven.config` does not apply — this reproduces exactly
IntelliJ's repo list:

```bash
cd /tmp
for a in com.clevertap:bouncy-castle:1.5.4 \
         com.clevertap:clevertap-smack:1.0.0 \
         gnu.trove:trove:3.0.3 \
         com.amazon.athena:athena-jdbc:3.2.0 \
         software.amazon.awssdk:athena-streaming:2.0 \
         com.simba.googlebigquery:googlebigquery-jdbc42:1.6.3.1004 \
         com.vowpalwabbit:vw-jni-native-linux-aarch64:9.11.1 \
         io.confluent:kafka-avro-serializer:5.3.0; do
  printf "%-60s" "$a"
  ~/repos/Ultron/mvnw -q -s ~/.m2/settings.xml \
    org.apache.maven.plugins:maven-dependency-plugin:3.6.1:get \
    -Dartifact=$a >/dev/null 2>&1 && echo OK || echo FAIL
done
```

All 8 must print `OK` before you bother reloading the IDE.

---

## What Makes It Come Back

| Trigger | Action |
|---|---|
| `m2-settings.xml` gains a repo upstream | Re-run the `sed` one-liner |
| New clone, or the repo is moved | Re-run it — the `file://` URL is absolute to *one* clone |
| Reboot wipes `/tmp/ultron-m2-settings.xml` | CLI only; any `./mvnw <goal>` recreates it. `~/.m2/settings.xml` is permanent |
| New jar added to `.mvn/legacy_deps` | Nothing — same repo id, already covered |
| One stubborn artifact after all the above | `rm ~/.m2/repository/<path>/_remote.repositories` — an untracked artifact is treated as locally installed and accepted by *any* repo list |

### Relation to the documented VM workaround

`ultronmux/docs/VM-Setup-Guide.md` and `ultronmux/ansible/README.md` say:

```bash
cp -r .mvn/legacy_deps/* ~/.m2/repository/
```

This works for the same reason as the last table row — a plain `cp` does not create
`_remote.repositories`, so provenance tracking never engages. It is the blunt version of the
fix, and it is **incomplete**: it covers `project-local` only, so anything from `confluent`
(`io.confluent:kafka-avro-serializer`) still fails in the IDE.

---

## Habits

- Never make `-U` the first move on a "not found". Check `ls ~/.m2/repository/...` first —
  it is instant, and a present jar rules out the entire network hypothesis.
- When CLI and IDE disagree on a Maven project, compare **effective settings**, not poms:
  `./mvnw help:effective-settings` vs. the same command with `-s ~/.m2/settings.xml`.
- `.mvn/maven.config` is the first file to read in any unfamiliar Maven repo. It silently
  changes every CLI invocation (here: `-s`, `-T1C`, `--no-transfer-progress`) and is invisible
  to IDE importers.
