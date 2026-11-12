# IDE Profile Refactor Design

> **Superseded.** This is James's partial draft, added October 27, 2026 (#71). It was expanded into the full
> design [ide-profile-refactor-2026-10-28-final.html](ide-profile-refactor-2026-10-28-final.html),
> which was built as PR #106 (merged October 29, 2026, Pacific) and which has its own status
> note on where the code has since moved on. Nothing below describes the current code; see
> `README.md`, `core/README.md` and `docs/hosting.md` for that. Kept for history.

> **How to use this document.** This is a partial design document. It sets out the general direction and the initial choices for a complete refactor of the IDE profiles in this repository, but it is not a finished design: some parts are settled decisions, some are sketches, and some are open questions. Treat the settled decisions as requirements, and the rest as starting points to develop.
>
> The result of this session should be a complete design document, rendered as HTML (use the `doc.generate-design-doc` skill if it is available) and saved in `docs/designs/`, which we will then iterate on together. To get there, read the current profiles and code to ground each part of the design in what exists today, fill in the parts sketched here, and turn the open questions into either decisions with their reasons or explicit questions for the owner. Where this document conflicts with itself or with the code, say so in the design rather than silently picking one side. This session is for designing, not implementing: don't change the profiles' code.

Over the past few weeks, substantial work has been done on the IDE, especially in terms of UI/UX. This work is beginning to solidify into a form far from what the IDE profiles were initially designed for. In this session, we will be redesigning the profiles to fit the likely shape of the IDE, and to ensure extensibility into the future. This will entail (with some overlap between items):

* Defining the interface between the orchestrator and the IDE/human user
  * Defining how the orchestrator profile expects to receive IDE context information and how it knows how to interpret that information
  * Defining the tools the orchestrator needs
* Consolidating/refining the semantic representations of work and knowledge
  * Those shared between the agents and the IDE/human
  * Those which are internal to the agents
* Adding/improving the skills and scripts the agents have
* Refining the memory system

Let's go through each of these points. 

## Orchestrator <-> IDE Interface

The orchestrator needs correct, up-to-date, clear information about how it interacts with the IDE. Aside from chatting, which happens via the bridge, there are two components to this: a) the way the orchestrator is told about the current IDE state and associated context about the user and what they're doing and b) the tools the orchestrator will need to interact with the IDE. 

### IDE Context

For the first component, there's currently a lot of information sent from the IDE in the `instructions` string that's sent every message, including some "information" that's really more workflow/work wode settings in the chip mode data. This is getting pared down slightly, but as part of this redesign we'll decide what information we want from the IDE in general, then how that will be conveyed to/steer the agent. I'll divide the information the agent should receive from the IDE into two categories: session-level information and message-level information. The former is delivered once, at the start of the session, and comprises information like the draft policy being worked on, the chip mode, the league and policy names and IDs, and so on: things which define the start conditions of the session and are expected to steer the overall flow of the session without changing frequently. The latter includes information like the current tab the user is on, what surface they're in, on-screen references, highlighted text, and so on.

We should include a YAML or JSON file fitting a schema which defines each of these pieces of information, what they are, what they do, how they're formatted, where they're expected, and so on. This file allows us to easily modify the IDE context the profiles expect. In conjunction with this, we should modify the bridge so it expects a chat request record to include this context in a valid format (probably versioned), then knows how to take this context information plus the user's chat body and format them into a message to send to the Hermes API. At the beginning of a session, the SOUL.md file should be updated with procedurally-generated instructions for what IDE context it will receive and how to use it. 

As mentioned above, currently the IDE sends us some context. We should look at that, but not over-index on it. The goal is for the profiles to be able to be used with a variety of frontends over the bridge, using the schema-based context specification file to be able to understand the context they're getting.

### IDE Tools

Similar to the context, the IDE allows the agent to display different kinds of user to the information and to interact with the page the user is viewing, such as by changing tabs, reporting on progress, or creating and showing documents. Likely there will be a variety of such capabilities in any IDE the profiles are attached to. Just as the context the IDE provides the profiles should be extensible and easily definable, the tools should too. Since tools are (or at least, can be) self-documenting, this is somewhat easier. Most likely we should just expect the IDE to serve an MCP which describes the UI interaction tools in a specific section. 

As we'll talk more about later, however, it's likely that many of the UI elements that the agent will be manipulating will rely on ontological primitives shared between the user and the orchestrator, and possibly the researcher as well. As such, we will expect the tool descriptions (via MCP or otherwise) to use the same means of specifying ontological entities such as Goals, Tasks, or Experiments. How these are specificied should be dictated here by the profiles, and we'll work on that in the next section.

## Semantic Representations

In order to create a formal connection between model and harness, we define a set of semantic representations of knowledge the agent has and work the agent is, has, or will do. These representations serve as a sort of bridge: their formal specificity allows the harness to use them programatically, such as by creating new ones, managing existing ones, modifying them according to fixed schemas and scripts, controlling harness flow based on them, and even validating, verifying, fixing them. Simultaneously, that same formal specifity works with the flexible, natural language-based semantic content as well as the typed metadata to give the LLM powering the harness the ability to work with, modify, and reason about said semantic content, and to have a clear understanding of the relationships that content has to other kinds and instances of semantic representations. However, having too many formal representations unnecessarily constrains the agent; an ideal IDE harness will provide the minimal set of broadly-applicable semantic representations to allow the harness to effectively guide and manage agent work and knowledge while still allowing the agent to leverage it strengths as it sees fit and to organize knowledge as it desires. 

An additional consideration is that formal semantic representations can also be used to connect the UI/UX of the IDE to the agent. In the same way the formal specifications of semantic representations allow the harness to operate on them programatically, they allow UI/UX elements to visualize them, and allow the IDE code to tie into the harness and perform various operations on the representations directly. Thus, it is desirable to choose semantic representations which work well for humans using the IDE. 

In sum, we want a set of formal semantic representations for our profiles which:

1. Allows the harness to manage the agent's knowledge
2. Allows the harness to manage the agent's workflow
3. Allows the agent to manage its own knowledge
4. Allows the agent to manage its own workflow
5. Includes a subset of representations which are intuitive to and useful for humans

Before we get into the representations themselves, let's set out some core concepts about the representations themselves. 

* **One file per item.** Each item is a Markdown file with YAML front matter: the typed metadata goes in the front matter, the natural-language content in the body. This permits both the programatic aspects the harness relies on and keeps a free-form, LLM-native body for content. It also keeps individual pieces of knowledge separate. 
* **Changed only through tools.** Agents reach the stores only through tools; the stores are kept in protected folders that the agents' general-purpose tools can't reach (see Storage). Every write takes a lock, re-reads the file inside the lock, validates, writes atomically and makes its own git commit. A refused write changes nothing and returns a clear reason why to the agent. This makes sure changes go through the harness, which ensures the integrity and reliability of the metadata. Additionally, it allows tool outputs to be tailored and refined we can improve context quality.
* **Derived values are computed, not stored.** A Goal's Tasks, a claim's freshness and whether a claim is contested are all computed when read, so they cannot drift. This is another mechanism to ensure metadata reliability. Rather than relying on the model to religiously and accurately keep metadata up-to-date with tool calls, the tool calls themselves, both reading and writing, should ensure metadata remains correct wherever possible. The exception is agent-driven metadata such as see also links, 
* **Legible without chat history.** Each item must make sense to a fresh agent with no conversational context (though the system prompt context can and likely will be important). The goal is for the system prompt to define the semantic representations, and for the instances to carry the information necessary for the agent to make sense of them quickly. This can mean they directly say everything they need to, but also includes things like see also links, references to evidence, claims, tasks, etc., which allow the agent to know where to look next to understand the item.

### Representations of Agent Work

Work is represented by **Goals**, **Tasks**, **Sessions** and **Versions**.

#### Goals

A Goal is an outcome being worked toward, with an end state that can be judged as complete or incomplete. The fundamental purpose of a Goal is to keep track of long-term objectives the system needs to accomplish, whether these are from the user's instructions or generated internally as part of a broader effort (potentially even as part of working on a broader goal). Goals allow the human user or the system itself to select some desired outcome, improvement, piece of knowledge, etc. and then have the system work persistently on achieving it. They serve as projects which guide the creation of Tasks and inform agent Sessions for those Tasks, and as targets for long-running auto-research. They also act as the destination for updates to the Goal, a hub for associated knowledge, and a single point of contact for the human to check up on.

| Field | Meaning |
|---|---|
| `id` | `g-<slug>` |
| `title` | Short name of the outcome |
| `end_state` | What must be true for the Goal to be complete, stated so it can be judged |
| `reason` | Why the Goal exists, usually quoting the human's request |
| `status` | `active`, `parked`, `completed` or `abandoned` |
| `status_reason` | Required when `completed` or `abandoned` |
| `created`, `created_by` | Date and session |
| `judgement` | Instructions for judging the end state |

Goals allow the harness to keep the system running and focused: when a Goal doesn't have any active tasks, it should flag the system to wake the agent and ask it to manage the Goal: either by creating new tasks, updating the Goal itself, marking it parked or closed, asking the human for help, and so on. When the agent wakes, it can use the list of Goals to orient itself, and it can add, edit, and remove them for its own purposes or based on chats with the humans. The UI the human sees can render the goals and allow the human to interact with them, show progress and work on Goals, and so on. 

#### Tasks

A Task is one bounded piece of work under exactly one Goal, of one type, with an end condition. A Task is defined by:

- a description of the Task reasoning behind the Task
- the type of the Task,
- the Goal it's working towards,
- its status: one of unclaimed, in-progress, or complete.

We're using Task here as the basic unit of *long-running, autonomous* labor, and specific Task types take on more specific roles. All Tasks, however, should be *focused*: the Task should identify a specific deliverable, a fixed set of steps, or a time- or effort-bounded end condition. 

Each Task type is a file (`task_types/<type>.md`) holding the instructions a Researcher gets for that type, any extra fields a Task of that type must have, and a completion checklist: the fields the completion tool requires before it accepts the Task as complete, such as the claims written or the Version made. This follows the webDiplomacy playerADE, which starts with three types:

| Type | What it does | Its deliverable |
|---|---|---|
| `research` | Builds grounded knowledge by reading replays, code, transcripts, docs and data and running analyses; it changes no policy code | Claims, written or updated |
| `improvement` | Makes one policy change and tests it on the live field: one Experiment, from hypothesis to verdict | The Experiment, the Version it tested, the claims from its verdict, and whether the Version should be proposed for submission |
| `ideation` | Proposes what to try next for a Goal that has no open Task, grounded in the Goal's claims and in what was already tried, refuted claims included; it changes no code | Two to five new Tasks |

A new type is a new file, so a new kind of work doesn't need a code change. Unlike in the playerADE, types carry no budgets (see Submission and Credits), and whether a type needs the user's approval before it starts is a user setting (see User Settings).

| Field | Meaning |
|---|---|
| `id`, `goal`, `type` | Identity, parent Goal and Task type |
| `title` | Short name, for the agents |
| `status` | `unclaimed` → `in-progress` → `complete` |
| `outcome` | On completion: `succeeded`, `failed`, `inconclusive` or `abandoned` |
| `result` | On completion: what came of it |
| `waiting_on` | Set while an in-progress Task is blocked: a question for a person, or `process <n>: <reason>` for a long-running command |
| `claimed_by` | The Session currently holding the Task |
| `sessions` | Every Session that has worked the Task, in order; almost always just one (see Execution Lifecycle) |
| `worktree`, `branch` | The Task's own git worktree and branch |
| `created`, `created_by` | Exact timestamp (microseconds, for ordering) and session |

Like all semantic representations, Tasks allow the harness to keep the system running smoothly. In this case, they allow the harness to know what kinds of work needs to be done, they allow the system, either the LLM or the harness or even the user, to specify discrete units of work, they allow keeping track of the progress of these units of work, and, of course, they allow dispatching new agent Sessions to handle this work. Tasks should be automatically generated by the orchestrator agent from Goals when appropriate. Notably, a Task in this sense is *not* the same as a Kanban task for the Hermes Kanban system; rather, each Task has one *associated* card in the Hermes Kanban which links out to the actual task representation. 

As noted above, one Task corresponds to one agent Session, and this is an explicit goal. Each task should be handled, end to end, by a single Session, and each Task should be scoped properly so that this can happen effectively, meaning the Session should require very few, ideally zero, compactions. The distinction between a Task and a Session is that the Task keeps track of the discrete piece of work itself: it exists before, during, and after the work is done, standing as a record that the work needs to be done, is being done, or was done (or wasn't!) for the harness and the UI and the agent itself. It's also worth noting that not all work will be done via Tasks; rather, Tasks are specifically meant for semi- or fully-autonomous background work which happens without the user being specifically aware of it. Thus, Sessions *can* exist without Tasks, as we'll see below.

#### Sessions

An agent Session is one "run" of an agent, which constitutes a series of LLM generations, tool calls, etc. Fundamentally, a Session is a single "trace," but can be on-going, not just historical, and a Session need not be interactive with the user, though it can be. Each Session stores its history, including generations, user chats, and tool calls, and can be (but does not have to be) tied to a Task and/or a Goal. Sessions carry their status, which can be `running`, which means the session is hot, there's a real agent runtime currently running the session and doing work, whether that's waiting for call responses, tool responses, etc., `stalled`, which means the session is warm and there's a real agent runtime with the context loaded, but it's waiting on a user response or a response from another agent, or `stopped`, which means it's cold and there's no active agent runtime with the context loaded. Sessions should also carry metadata about their model, reasoning, etc., as well as the initial system prompt, conversation IDs, etc. Note that Sessions are the exception to the single-file rule because Session history should be in a separate, standard JSONL file. 

Sessions exist mostly for the harness and the UI, since they allow managing the ruinning agents. In particular, Sessions allow for context management, interaction with the user outside long-running Tasks, Task delegation, etc. The IDE, including the UI and the profiles, need to manage Sessions, start them for tasks, on crons, etc. 

#### Versions

A Version is one preserved state of the policy code: a commit on a tagged branch of the code repository, with a record of where it came from and what it is for. Versions are what make the work reproducible. Every Task or Experiment that changes code ends with that code preserved as a Version, whether or not the change won, so any result, claim or submission can be traced to the exact code behind it, and any earlier version can be rebuilt, compared against, or returned to.

| Field | Meaning |
|---|---|
| `id` | `v-<6 hex>` |
| `summary` | What this Version changes, in one sentence |
| `branch`, `tag`, `sha` | Where its code is: its branch and tag in the code repository, and the exact commit |
| `based_on` | The Version it was built from |
| `task`, `experiment` | The Task and Experiment that created it, if any |
| `uploaded` | The platform's id for it (its PolicyVersion) and when it was uploaded, once it has been |
| `goals`, `claims`, `experiments` | Related Goals, claims and other Experiments, such as ones that used it as a baseline |
| `created`, `created_by` | Date and Session |

The body says what changed and why, and how the Version was built and tested.

One Version is the **champion**: the one the league shows competing for the user's Player. Which Version that is, since when, and why it was submitted is kept in a single champion record in the store, and it is part of every Session's briefing (see Memory), so the current champion is always in every agent's context. The harness keeps it current. A submission updates it, and the harness also checks the league, so if the champion changes some other way, for example because the user submitted from outside the IDE, the record is updated, a Version is created for the new champion with whatever is known about it, and the Orchestrator is told.

### Representations of Agent Knowledge

Knowledge is fundamentally represented as **claims**. Each claim carries typed **evidence** links. **Experiments** are a separate, more structured record of what the agent has tried; what they show clearly becomes claims.

#### Claims

A claim is one narrow, focused statement of something the system believes, stated so that it could turn out to be wrong, and linked to the evidence for and against it. Claims are the system's beliefs and knowledge: what it has learned about the game, the opponents, the platform, its own policies, and the user's preferences. The fundamental purpose of a claim is to make knowledge small enough to find, check, and update one piece at a time. A long lessons document or a project log mixes many ideas of different ages and quality together, so an agent reading it can't tell which parts are still true or what they rest on. A claim holds one idea, says how recently it was confirmed, and points to what an agent can look at to check it.

Claims replace the scattered knowledge stores the profiles have today. The current ontology (`ontology/ONTOLOGY.md`) has separate claim, hub and lesson records, each with its own states; most of what these hold is a belief with some evidence behind it, which is exactly what a claim is. Hypotheses are not beliefs yet, so they move into experiments instead. Categories of knowledge which used to be separate files, such as improvement ideas, best practices, or user preferences, become tags on claims rather than new stores.

| Field | Meaning |
|---|---|
| `id` | `k-<6 hex>`, from a hash of the summary and the creation time, so parallel writers never collide and no central counter is needed |
| `summary` | The claim in one sentence, at most 40 words |
| `tags` | Keywords from a shared vocabulary, including the ids of the Goals the claim serves |
| `status` | `active`, `superseded` or `refuted` |
| `superseded_by` | When `superseded`, the claim that replaces it |
| `created` | Date |
| `evidence` | One or more typed evidence links (see below) |
| `see_also` | Other claims worth reading alongside this one; these are for navigation and do not count as support |

The body holds the claim itself in full: the detail, the numbers, the conditions under which it holds, and what would change our mind. It is at most 500 words, and preferably much less. If a claim needs more than that, it is probably more than one claim.

Several of a claim's most important properties are computed from its evidence every time it is read, rather than stored, so they can never go stale:

* **Freshness:** how recently new positive evidence for the claim was found and connected to it, that is, the newest date among its supporting or qualifying evidence. A claim nobody has re-confirmed in weeks shows its age.
* **Contested:** whether contradicting evidence has arrived since the claim was last confirmed. A contested claim is still active, but an agent relying on it should know it is in doubt.
* **Evidence counts:** how many links support, contradict and qualify the claim.

A claim ends its active life in one of two ways. It is **superseded** when a better claim replaces it, for example a more precise or more current statement of the same idea. It is **refuted** when the evidence kills it, and the killing evidence is attached when it is retired. In both cases the claim is kept and stays findable. Claims are never deleted, because a refutation is an answer too: the most expensive mistake a long-running research system makes is re-walking a dead end that nobody wrote down.

The harness enforces the structural rules on every write, and refuses a write that breaks one, with the reason:

* The summary is a single sentence of at most 40 words, and the body is at most 500 words.
* Every claim has at least one evidence link, and every link resolves: the thing it points at exists.
* Claims cannot support themselves in a circle. Claim-to-claim evidence must not form a cycle, though see-also links may, since they are only for navigation.
* Every claim named in `see_also` or `superseded_by` exists.

The rest is guidance for the agent on how to write good claims, given in the system prompt and in the write tool's description: one idea per claim; state it so it could be wrong; give every number its comparison, sample size, and date; search before writing, and add evidence to an existing claim rather than writing a near-duplicate; and tag a claim with the Goals it serves.

The guidance also covers what the user tells the system about themselves. Their preferences, standing instructions, coaching and opinions about their policy are kept as claims, tagged as the user's preferences, with the chat messages they came from as evidence. When the user says something that bears on an existing preference claim, the agent adds the message as evidence or qualifies the claim; when they change their mind, the old claim is superseded by a new one, so the system always knows what the user wants now and what they wanted before. The user doesn't have to state preferences in any particular way: it is up to the agent to notice when something they said is worth keeping.

Claims give the agent a knowledge base it can actually use. Its search tool takes a grep-style pattern and, rather than returning raw matching lines, returns a short list of the matching claims, each with its summary, tags, the matching line, its freshness, and its evidence counts. It also lists claims which didn't match but which several of the matched claims point to through their see-also links, so related knowledge surfaces even when it uses different words. Refuted and superseded claims are listed too, marked as such, at the bottom. From there, the agent reads the claims it needs in full, inspects their evidence, and writes, updates, or retires claims as it learns. The agent should never read or write the claim files directly, only through these tools, so that every change is validated.

For the harness, claims are what lets it check the agent's work: a research Task can be required to leave behind the claims it produced, and the harness can verify that those claims exist and that their evidence resolves. For the human, claims are the "what we know" of the system. The IDE can show the claims tagged with a Goal on that Goal's page, with how fresh each one is and whether it is contested, and the user's own statements in chat can become evidence on claims about their preferences.

Evidence that cites a Task's work, such as a measurement, a build or a line of its code, always resolves, because every Task's code is preserved as a Version whether or not it is later merged into `main` (see Versions and Storage).

#### Evidence

Evidence is what makes a claim checkable. Each piece of evidence is a typed link from a claim to something an agent can go and look at: a replay, a line of code, a document, a measurement it can re-run, something the user said, or another claim. Evidence is not a standalone representation; it lives inside the claim it supports. The governing rule is the one the whole knowledge system rests on: **evidence the agent can't look at itself isn't valid evidence.** If a claim rests on something no agent can open and read, nobody can check it, and the knowledge base slowly fills with beliefs whose basis is lost.

| Field | Meaning |
|---|---|
| `type` | One of the registered evidence types (below) |
| `ref` | Where the evidence is, in that type's reference format |
| `stance` | Whether the evidence `supports`, `contradicts` or `qualifies` the claim |
| `added`, `by` | When it was attached, and by which Session or person |
| `note` | What the evidence shows, usually with its numbers |

The stance is what drives a claim's computed values: supporting and qualifying evidence refreshes a claim, and contradicting evidence newer than that makes it contested. Adding new evidence to an existing claim is therefore how the system records that a belief was checked again, and it is the main way a claim stays fresh.

To enforce the governing rule, evidence types are a real registry in code rather than a convention. Each type defines:

* a **reference format**, so a link can be written and parsed reliably;
* a **validator**, which checks that the reference resolves, that is, that the thing it points at exists;
* an **inspector**, which shows the agent what the evidence says, through a single inspect tool;
* a pointer to the **skill or tool for reading it in more depth**.

A new kind of evidence is valid only once it has all four. Some types are general and belong to the profiles themselves; others depend on a particular game or frontend and should be provided by it, in the same way the IDE describes its own context and tools:

| Type | Points at | Provided by |
|---|---|---|
| `claim` | Another claim | Profiles |
| `code` | A file and line range at a specific commit; inspection also shows whether those lines have changed since | Profiles |
| `doc` | A section of a document in a repo, or a web page with its retrieval date and the quoted passage | Profiles |
| `transcript` | A message in an archived agent Session, with the quoted text and its hash | Profiles |
| `measurement` | A command, the commit it ran at, and its saved output, so it can be re-run | Profiles |
| `experiment` | An experiment record (see below) | Profiles |
| `version` | A Version: a preserved state of the policy code, uploaded or not (see Versions) | Profiles |
| `replay` | An episode, optionally narrowed to a phase and seat | Game |
| `chat` | A message in the IDE chat, with the messages around it | IDE |
| `annotation` | A note the user left on a replay | IDE |

Some evidence will rot unless the system protects it, and the design has to account for that. Replays and other bulky artifacts usually live in untracked folders that get cleaned up, so the episodes that claims cite should be pinned, or at least recorded with what's needed to download them again. Agent transcripts are deleted or reformatted by their harnesses over time, so Sessions should be archived, and transcript evidence carries the quoted text and its hash so the quote stays checkable even if the archive format changes. The same applies to the user's chat history in the IDE, which is often the only record of what they asked for.

#### Experiments

An experiment is the record of one thing the agent tried: a hypothesis, the method used to test it, and what happened. A hypothesis is not a claim. Claims are what the system believes and has support for, and an idea under test has neither yet, so it lives in its experiment until the evidence is in. Experiments are more structured than the other representations: the hypothesis, methods and results are separate structured fields, so the tools and the harness can read, check and return each part on its own.

Experiments exist for several reasons:

* **A record of everything tried.** Every test is kept and searchable, including the failed and abandoned ones, so the agent can see what has already been tried before it proposes the same idea again.
* **Rigor.** The predictions and the decision rule are written down before anything runs. This makes it hard to run a test whose result would look like a success either way, or to move the bar after seeing the numbers.
* **Orchestration.** When an experiment needs background work, one Task runs it. Creating an experiment can create its Task, and the harness briefs the Session working that Task from the experiment: what to build, how to evaluate it, and how to judge the outcome.
* **Context management.** Experiments are created, read and updated only through tools, so an agent loads only the experiment, or the part of it, that it needs, instead of whole files or logs.
* **Bookkeeping.** An experiment records the versions it compared and the runs it launched, so every policy version and evaluation run can be traced to the reason it exists.

Experiments replace the profiles' current hypothesis, experiment and result records, which split one test across three records with their own states.

| Field | Meaning |
|---|---|
| `id` | `x-<6 hex>`, generated the same way as a claim id |
| `summary` | What is being tested, in one sentence of at most 40 words; this is what search results show |
| `goal`, `task` | The Goal the experiment serves, and the Task that runs it, if any |
| `tags` | Keywords from the same shared vocabulary as claims |
| `status` | `proposed` → `running` → `confirmed`, `refuted` or `inconclusive`; or `abandoned` |
| `created`, `closed` | Dates |
| `related_claims` | The claims the experiment builds on or was checked against |
| `related_experiments` | Earlier experiments it follows from, repeats or revises |
| `hypothesis` | The idea under test (below) |
| `methods` | How it is tested and judged (below) |
| `results` | What happened (below) |

The three structured sections hold these fields:

| Section | Field | Meaning |
|---|---|---|
| `hypothesis` | `statement` | The idea, stated as a mechanism: "X happens because Y, causing Z" |
| | `if_true`, `if_false` | What we expect to see if the hypothesis holds, and if it doesn't. These must differ, or the experiment can't tell us anything |
| | `motivation` | Why the idea seems worth testing, citing the related claims and experiments |
| `methods` | `baseline`, `candidate` | The Versions being compared (see Versions); the candidate is empty until it is built |
| | `procedure` | What to build or change, and how to run the evaluation |
| | `metrics` | What is measured, the sample size, and how noisy each metric is |
| | `decision_rule` | The rule the result will be judged by |
| `results` | `runs` | The ids of the evaluation runs, added as each one starts |
| | `measurements` | The numbers, each with its baseline and sample size |
| | `interpretation` | What the result means, read against the decision rule |

An experiment moves through its states like this:

1. **Proposed.** The summary, hypothesis and methods are written, and can still be edited. Nothing has been built or run.
2. **Running.** The hypothesis and methods lock when the experiment starts. If the design has to change, the experiment is abandoned and a new one is created that lists it in `related_experiments`. Results are added as the runs come in.
3. **Confirmed, refuted or inconclusive.** The result is read against the decision rule, and nothing else. If the rule turns out to be the wrong one, the honest verdict is inconclusive, and the next step is a better experiment, not a new threshold. An experiment stopped before a verdict is **abandoned**, with the reason. Experiments are never deleted: a failed test is exactly the record that stops the system from running it again.

Closing an experiment does not change any claims by itself. The agent writes whatever the experiment showed clearly enough to believe as a new claim, or as evidence on an existing one, with `experiment` evidence pointing back to it. That includes negative results: "X does not help against Y" is a claim worth having. Reading an experiment also lists the claims that cite it, so the link works in both directions.

The harness enforces the structure on every write, and refuses a write that breaks a rule, with the reason:

* An experiment can't start running until its hypothesis and methods are filled in, including the decision rule.
* Once it is running, its hypothesis and methods can't change.
* It can't close without at least one run, its measurements, and an interpretation.
* Every reference resolves: the Goal, the Task, the versions, the runs, and the related claims and experiments.

Because the parts are separate, the harness can do more than check them. When a Session picks up an experiment's Task, the harness gives it the hypothesis, methods and decision rule, with the summaries of the related claims, so the Session knows exactly what to build, how to evaluate it and how to judge the outcome. When the Task finishes, the harness checks that the results are filled in and reminds the agent to record its findings as claims.

The agent works with experiments through tools to create, read, search and update them, and never touches the experiment files directly. Reading can return a single section, such as only the methods. Search works like claim search: it takes a grep-style pattern and returns a short list of matching experiments, each with its summary, Goal, tags, status, and the matching snippet. It also lists their related experiments, so earlier attempts at the same idea surface even when they use different words. For the human, experiments are the clearest view of what the system has tried and why: the IDE can show a Goal's experiments as "what's been tried", each with its hypothesis in plain language and its verdict.

### Storage

Each sandbox workspace is for a single Coworld. The workspace holds everything for that one game: its Coworld mixin, the policy code, the work and knowledge representations, and the git history of all of it. The profiles should expect exactly one game in their workspace, and never have to choose a game, or scope a record, a Task or a search to one. The user's other Coworlds get workspaces of their own. This replaces the multi-game support added in #46 and #47 (the `game` argument on cards and records tools, `game_list`, `game_install`, and one canonical repository per game on a host), which can be removed.

The workspace has two parts. The **code repository** is a git repository holding the policy code, the Coworld mixin and everything else the agents work on directly. The **store** holds the representations: Goals, Tasks, Sessions and their archived transcripts, Versions, claims, Experiments and the user's settings. The store sits in the workspace but outside the code repository, and it isn't branched: every write goes through a tool, takes effect immediately, and is visible to every Session and to the UI as soon as it is made. The store keeps its own history, since every write is its own commit, but there is only one copy of each record, so there is nothing to merge.

Code is where branching happens. Each Task gets its own worktree and branch in the code repository, made when the Task is claimed, and the Researcher Session for that Task works only there. The Orchestrator does the same when it changes code itself: it makes a short-lived worktree and branch for the change rather than editing `main` directly, so every code change takes the same path.

Because the store holds one copy of each record, the harness locks them. A Task and a Session can each be held by only one agent at a time: the Researcher Session that claims a Task holds it until the Task completes or is released, and nothing else can work on that Task or in that Session meanwhile, so only the holder ever writes that Task's or Session's record. Records that several Sessions share, such as Goals, claims and Experiments, aren't held, but every edit takes the record's lock, re-reads it and validates it before writing, so two Sessions changing the same record apply their edits one after the other, never over each other. These locks are enforced by the harness, not left to the agents' good behaviour.

The store is kept in protected folders that the agents' general-purpose tools can't reach. In Hermes, the shell, the file tools and `execute_code` all run in one configurable terminal environment (local, Docker, SSH, Modal and others), and the file tools work through that same environment rather than on the host directly. The profiles run that environment with the code repository and its worktrees mounted but not the store, so the store doesn't exist as far as any general-purpose tool can tell, whatever command an agent runs. The representation tools run in the Hermes process itself, outside that environment, and are the only code that can open the store. This is stronger than a hook: today's researcher guard parses shell commands to block `git push`, which a one-line Python script gets around. Two lighter layers stay as backstops: a hook refuses file-tool calls on store paths with a clear reason, and the merge wake validates every record it merges. If a host can't run the terminal environment separately, for example a sandbox without Docker, the fallback is file ownership: the store belongs to a different operating-system user, and the representation tools reach it through a small helper that runs as that user.

A special wake of the Orchestrator, the **merge wake**, runs when a Task completes, or when the Orchestrator finishes a change in its own worktree. It handles one branch at a time and does no other management, so its work is never left half done in the middle of a conversation. Its most important job is making sure the code that was created and tested is preserved. For each branch it:

1. Preserves the branch's code as a Version: it tags the branch and creates the Version record, with its commit, its Task and Experiment, and the related Goals and claims (see Versions). Every branch with code changes gets a Version, whether or not the change won.
2. Merges the branch into `main` when its work should become the base for later work: a Version that is becoming champion, or changes such as tools, mixin skills and fixes that other work should build on. Conflicts are resolved automatically wherever possible, and the builds and checks are re-run after a merge. A candidate that lost its Experiment is not merged; it stays available as its Version.

If a conflict can't be resolved, `main` is left unchanged, the Version is still kept, and the Orchestrator either creates a Task to reconcile the work or asks the user.

## IDE State

All state the profiles need or use is kept in the workspace. The IDE keeps only UI state, such as layout, open tabs and what the user is looking at, and never state that the agents depend on. The IDE's MCP server as it stands today is not a constraint here: its `discovery.*`, `experiments.*` and `projects.*` operations keep their own records, and they should evolve to fit this design rather than the other way round.

The bridge is responsible for keeping the IDE's UI up to date with the latest work and knowledge, so the UI can always show it accurately. The UI may keep its own copy of Goals, Tasks, Experiments, Ideas (most likely just the hypothesis part of an Experiment) and so on, for display. That copy is a cache, never the authority: whenever an agent uses a tool that changes a representation, the tool or the bridge updates the IDE automatically, so the agent never has to tell the UI about its own work. When the user changes something from the UI, for example parking a Goal or rejecting an Idea, the change goes through the same tools, by way of the bridge, rather than into an IDE store.

This needs more design. The open questions include whether updates are pushed by the tools or pulled by the bridge watching the store, how much of each representation the UI receives, and how the user's edits from the UI are attributed and validated.

## Memory

The representations are the memory system. What the system knows lives in claims, what it is doing and has done lives in Goals, Tasks and Experiments, and what happened in each run lives in Sessions. There is no separate memory store beside them, because anything kept outside the representations has no evidence, no freshness, and no place in the workspace's history, and is invisible to the other profile and to the UI.

Hermes's built-in memory is turned off for both profiles. Today it is two small files per profile, `MEMORY.md` for the agent's notes and `USER.md` for the user, loaded into every Session's system prompt and stored in the profile's folder rather than the workspace. The Researchers already have it off, because notes without provenance would diverge between them; the Orchestrator has it on. In the new design, what would have gone into those files goes into the representations instead: the user's preferences and coaching become claims, with their own words in chat as evidence, and notes about how to work in this environment become claims or skills.

What built-in memory does well is put a few important things in front of the agent at the start of every Session, and that is kept, as a **briefing** computed from the representations rather than stored. When a Session starts, the harness adds a short summary to its context: the current champion, the Session's Task and Experiment if it has them, the active Goals, the claims about the user's preferences, and the freshest claims tagged with the Goals in play. Like the other computed values, it can't go stale, and it has a fixed size limit, so it points to claims and Goals rather than repeating them.

Sessions are the system's record of what happened. Their transcripts are archived in the workspace and are searchable, so an agent can find out how an earlier piece of work went, a recovered Session can be rebuilt from its history, and transcript evidence on claims stays checkable. A transcript is a source, not knowledge: anything worth keeping from it is written down as a claim or recorded on a Task or Experiment.

Within a Session, the aim is to need as little context management as possible. Tasks are sized so their Session rarely needs compacting. When it does, the summary should point to the representations the Session has been working with rather than restate them, since those can always be read again.

## Agent Roles

Previously we had two profiles for two different agents: the Orchestrator and the Researcher. This division of roles will remain, but the Orchestrator will become broader and more capable to reflect the fact that it will occasionally do work itself. The key difference between the Orchestrator and the Researcher profiles will be that the Researcher won't have direct access to the IDE's UI and should never communicate with the human user, and that the Researcher should be geared towards autonomous self-sufficiency, with the aim of running effectively in the background, working on Tasks and Goals when the user is away or focusing on something else. The Orchestrator is responsible for overseeing the Researchers, keeping work flowing by creating Tasks (and the corresponding Kanban cards which will spawn the researchers), reading Kanban card comments and blockers and such, and answering questions the Researchers have, including permissions questions. The Orchestrator is also responsible for interacting with the user, as it is today, but that can including using the Softmax and Coworld CLIs, writing and editing policy code, it's own skills, reading and updating knowledge from claims, etc. 

### Orchestrator

The Orchestrator has two jobs, and it should be good at both. It is **management**: it turns Goals into Tasks, watches their progress, answers the Researchers' questions and permission requests, and accepts their results by reviewing the Task's outcome, the Experiment's results and the claims left behind before the work is merged. It is also **front-of-house**: it is the only agent the user talks to, it reads the IDE context with each turn, and it uses the IDE tools to change what the user sees. 

As front-of-house, the Orchestrator also turns the user's ideas, coaching and statements into Goals, Tasks, Experiments and claims. The user talks or types however they like; it is the Orchestrator's job, not the user's, to understand what they mean and decide what, if anything, should be recorded and how. This matters because a Researcher never sees the chat, and neither does a later Session, so the user's direction only lasts once it is in the representations.

Today's Orchestrator is mostly a dispatcher. Its prompt is written around cards, receipts and promotion, and until recently it could reach nothing outside its own plugin tools (#57 gave it the terminal, file and code-execution toolsets). In the new design it does work itself whenever that is the better path. The rule of thumb is that work the user is waiting on, or work that is small enough to finish within the conversation, the Orchestrator does in its own Session: running the Softmax and Coworld CLIs, reading and editing policy code, writing its own skills, searching and updating claims, or creating an Experiment. When it changes code, it does so in a short-lived worktree of its own (see Storage). Work that is long-running, can run unattended, and fits a single focused deliverable becomes a Task for a Researcher. When the Orchestrator does substantial work itself, it still records it in the same representations, so the knowledge base and the record of what's been tried don't depend on which agent did the work.

Management keeps going when the user is away. Management wakes answer the Researchers, plan Goals that have run out of Tasks, accept finished work and create the next Tasks, all without a user turn (see Wakes). The Orchestrator also handles submission, making a version the league's champion: a Researcher that thinks a version should be submitted proposes it to the Orchestrator, and the Orchestrator asks the user (see Submission and Credits). Merging finished Tasks back to `main` is the Orchestrator's job too, in a wake of its own (see Storage).

### Researcher

The Researcher is a background worker built to run on its own. A Kanban card starts one Researcher Session for one Task, and that Session works the Task from start to end in the Task's own worktree. When the Task has an Experiment, the harness briefs the Session from it: the hypothesis, the methods, the decision rule and the related claims. The Session then knows what to build, how to evaluate it and how to judge the outcome without asking anyone.

The Researcher's measure of success is that it rarely needs anyone. It should finish its Task without supervision, choose its own procedures from its skills, and ask only when an answer is necessary to continue. Its questions go to the Orchestrator: they are recorded on its Task, and the harness carries them to the Orchestrator and mirrors them onto the Task's card. It never talks to the user and has no access to the IDE's UI. It does not create Tasks either; when it finds follow-up work, it proposes it to the Orchestrator, which decides whether it becomes a Task.

A Researcher's work is finished when its results are in the representations, not in its chat. It completes its Task with an outcome and a result, closes the Experiment with its results, writes what it learned as claims with evidence, and commits everything else to its branch. The Orchestrator's merge wake then preserves its code as a Version and merges it into `main` if it should be (see Storage). This is what lets the next Session, whether a Researcher's or the Orchestrator's, pick up where the last one stopped, with no access to the earlier conversation.

### Capabilities

Both profiles have full tool calling, shell and file access on their host: the terminal, background processes, reading and writing files, running Python, and every CLI the work needs. The one exception is the work and knowledge representations. Goals, Tasks, Versions, claims and Experiments live in the protected store (see Storage) and are read and written only through their tools, never as files, so every change is validated and an agent loads only what it needs.

Neither profile has a limit on turns or running time. Hermes's `agent.max_turns` and `agent.run_budget_seconds`, and the Kanban dispatcher's runtime limit, are removed; today the Orchestrator stops after 40 turns, and a Researcher after 150 turns or 90 minutes. A Session runs until its work is done, and the anti-stall wake (see Wakes) catches one that stops making progress. The Researcher can do everything the Orchestrator can except submission: uploading versions, launching XP requests, running the CLIs, and anything else its work needs, with no approval step. The Researcher is not supposed to submit, but nothing can fully stop it: it holds the same Player credential as the Orchestrator, and with a shell it could call the API directly. The design doesn't pretend otherwise. Instead it adds friction at every place a Researcher might try, and makes each point of friction explain the rule. The Researcher has no submission tool. Its role section says that submission belongs to the Orchestrator and the user, and how to propose one. And the submission commands it might run, such as `coworld submit` or `research softmax exec -- coworld submit`, are refused with a message that says the same thing: that a Researcher doesn't submit, and that it should propose the submission through its Task with its reasoning (see Submission and Credits).

The differences between the two profiles come from their roles, not from different capability levels:

| | Orchestrator | Researcher |
|---|---|---|
| Started by | A user turn in the IDE chat, or a wake | A Kanban card for one Task |
| Talks to | The user, and its Researchers through their Tasks | The Orchestrator only, through its Task |
| IDE context and tools | Yes | No |
| Tasks and Experiments | Creates both | Runs its Task's Experiment; proposes new Tasks and Experiments |
| Submission as champion | Proposes it to the user, and submits on their yes | Proposes it to the Orchestrator, with its reasoning |

## Execution Lifecycle

The Task record is the authority on a Task's state. Kanban cards exist only so that the Hermes Kanban engine starts a Researcher Session for a Task; they hold no state of their own that anything relies on. The harness keeps each card in step with its Task: creating a Task that needs a Researcher creates its card, and the card's status follows the Task's. No agent changes a card to change a Task. If a card is changed from outside, the Task still wins, and the harness brings the card back into line.

A Task moves through its life like this:

1. **Created.** The Orchestrator creates the Task, `unclaimed`, and the harness creates its card.
2. **Claimed.** The Kanban engine starts a Researcher Session for the card. The Session claims the Task, which takes the Task's lock and makes its worktree and branch.
3. **In progress.** The Session works the Task. While it has to wait, for an answer, an XP result or a long-running command, the Task's `waiting_on` says so, and the Session stays attached to the Task. Questions and answers are recorded on the Task too; the harness delivers them between the Researcher and the Orchestrator, and mirrors them onto the card only so the board shows them.
4. **Complete.** The Session completes the Task with its outcome and result, the lock is released, and the harness closes the card. The merge wake then preserves its code as a Version and merges what should be merged (see Storage).

Up to two Researchers work at once by default; the number is a setting (see User Settings). A Task should almost always be worked by a single Session, from claim to completion. When a Session dies, or its card is retried or restarted, the harness recovers that same Session if it can, resuming it from its history so the work continues with its context intact. Only if that isn't possible does the harness attach a new Session to the Task, started with whatever context history can be recovered from the original: its transcript or a summary of it, along with the Task, its Experiment, and the commits already on its branch. The harness watches for dead Sessions, such as a runtime that has stopped while still holding a Task, and handles them on its own; recovery never waits for an agent or the user to notice. A Task's lock belongs to its Session rather than to a process, so a recovered or replacement Session takes the lock over from the dead one, and no other agent can break it.

Researchers can do anything they are able to do whenever they need to: upload a version, launch an XP request, ask a question, or write claims and Experiments. None of these is tied to the card's lifecycle. Today, publishing a build waits for a completed card run (`orchestrator/src/orchestrator/board.py:347`), which forces a Researcher to finish its card before it can evaluate what it built; that coupling is removed. Only two things wait on something outside the Session: merging, which waits for the Task to complete, and submission, which waits for the user's yes.

Experiments don't need a Task. The `task` field is optional: the Orchestrator can create and run an Experiment within a conversation, and when an Experiment needs long-running background work, a Task is created to run it.

## Wakes

A wake is anything that starts or resumes an agent Session. The rule for all of them is the same: the harness watches the workspace and the platform, decides without a model call whether something has changed, and wakes the one Session responsible for acting on it. Nothing wakes a model just to check whether there is work. Every wake carries the event that caused it and an id for that event, so a wake that arrives twice is handled once, and the event is acknowledged only after it has been handled. A wake carries only its event, not a conversation: the representations hold everything a fresh Session needs.

| Event | What it wakes |
|---|---|
| The user sends a message in the IDE | The Orchestrator's Session for that conversation, through the bridge |
| A Task completes | The merge wake (see Storage) |
| A Task's `waiting_on` resolves: an XP result arrives, credits return, a question is answered, a long-running command finishes | The Researcher Session that is waiting, resumed directly |
| A Researcher asks a question or proposes a submission | An Orchestrator management wake |
| An active Goal has no unclaimed or in-progress Tasks, and isn't waiting on the user | An Orchestrator management wake, which creates an ideation Task for the Goal (see Tasks) |
| A Session dies while holding a Task | Harness recovery (see Execution Lifecycle), which wakes no model unless recovery fails |
| Once a day | The anti-stall wake (below) |

Every wake except a user turn is a **separate, short Session**. A management wake reads the representations, handles its event, and ends; it never continues a long-lived coordinator Session, as today's supervisor does, and it never adds to the user's conversation. This keeps each wake's context small, and keeps background work out of the user's chat. A Goal that several rounds of ideation haven't moved forward is parked rather than ideated on forever (the playerADE parks a Goal after three rounds whose Tasks all failed to succeed), and parked and completed Goals never wake anything.

A Researcher that is waiting for something slow, such as an XP result, doesn't keep a runtime running. Its Session stops, and the harness resumes it when the wait resolves, using the same recovery path as a dead Session.

The **anti-stall wake** runs once a day and checks that things are still moving. It looks at every Goal, Task and Session for liveness: active Goals with no recent progress, Tasks that have been in progress or waiting far longer than they should, Sessions that hold a Task but haven't done anything, and waits that never resolved. It fixes what it can, by recovering Sessions, releasing stuck Tasks or creating new ones, and asks the user about the rest.

The user can **pause** everything. While paused, no Task is claimed and no background wake runs, including the merge wake, recovery and the anti-stall wake. Events still accumulate, and they are handled in order when the pause is lifted. User turns are still answered while paused, since the user is the one who can lift it, but any work they start waits for the pause to end.

There is no special notification channel. If the IDE's MCP server offers a notification tool, the agents use it to tell the user about something while they are away, such as a submission proposal, a completed Goal or a question only they can answer; otherwise the user sees these the next time they open the IDE. Today's `ide_say` tool, which reaches only an older IDE page, is dropped.

## Submission and Credits

### Submission

**Submission** is making a version of the user's policy the league's champion: the version that competes for the user's Player. The profiles call this submission, not promotion, which in Softmax means a commissioner moving an entry between Divisions.

Most of today's restrictions on submission are removed. There is no statistical gate, no required Experiment verdict, and no multi-stage promotion record; instead, a submission needs the user's yes. Either profile can propose one:

1. A Researcher that believes a version should become champion asks the Orchestrator, through its Task, with its reasoning: what changed from the current champion, and the Experiments and claims that support it.
2. The Orchestrator, whether it is passing on a Researcher's proposal or making its own, asks the user, with a human-friendly explanation of what has changed and why it believes the new version should become champion.
3. If the user agrees, the Orchestrator submits the Version, and the champion record is updated with the Version, the time, and the reasons the user agreed to (see Versions).

Uploading versions and running XP requests are not submission and need no approval, since Experiments cannot be evaluated without them.

### Credits

All budgeting and spend controls are removed for now: Task budgets, episode reservations against a shared ceiling, and credit floors. A user who wants a limit sets it as a setting (see User Settings). In their place, both profiles get a way of reading the user's current XP request credit balance, and are taught how to use it: check the balance before launching a batch of XP requests, know roughly what a request costs, tell the user when credits are running low, and treat a request refused for lack of credits as a wait for the user rather than a failure.

## User Settings

Some of the user's preferences are rules the system should enforce, not just remember. A user might want Researchers to ask before spending more than a certain number of XP requests on one Experiment, to ask before posting to the Coworld's forum or wiki, or to have every improvement Task approved before it starts. Recording a preference like that as a claim isn't enough, since nothing makes an agent follow a claim. So the store also holds the workspace's **settings**: who may do what, and the limits the agents run under, in the manner of the webDiplomacy playerADE's `agents/authority.yaml` and `agents/config.yaml`.

When the user states a preference like this, the Orchestrator records it as a claim, as with any preference, and also changes the matching setting through the settings tool, so the preference takes effect. Only the Orchestrator changes settings, and only on the user's word. The tools enforce them: the XP request tool checks a spend threshold and turns a request over it into a question for the Orchestrator, a forum or wiki tool won't post without the permission a setting requires, and the Task tools hold back a Task whose type needs approval until the user gives it. Where a setting governs something an agent could also do from its shell, the enforcement is friction rather than a guarantee, as with submission (see Capabilities), and the friction explains the setting.

Settings start permissive: no spend limits, no approvals except for submission, which always needs the user's yes, and up to two Researchers at once. Pause (see Wakes) is a setting too. The set of settings should be easy to extend, so that a new kind of preference can be enforced without redesigning anything.

## Built-In Knowledge 

Both profiles should include pointers to several kinds of built-in knowledge: knowledge about the Softmax Universe and its vocabulary, knowledge about the profile's role and how to carry out that role, knowledge about how to improve a policy, knowledge about the particular game being worked on, and knowledge about the `softmax` and `coworld` CLIs and the Softmax API and how to use them as the Player identity. 

Only a small part of this belongs in the system prompt. The rule is that what every Session needs on every turn is loaded: who the agent is, the vocabulary, its role, and how to act as its Player. Everything else is a pointer to a skill, a doc or a URL that the agent reads when it needs it. Today's SOUL files are already about 3,700 words for the Orchestrator and 2,300 for the Researcher, so what is added here should mostly replace text rather than add to it. Where knowledge is maintained upstream by Softmax, the profiles should point at the upstream copy, or generate theirs from it, rather than keep their own version that slowly goes stale.

### Softmax Universe 

Both profiles should get a brief explanation in their system prompt/soul file that they are policy development assistants operating in the Softmax universe. The explanation should be no more than a few sentences: Softmax is a place where humans and agents work together to develop policies to participate in coworlds. Coworlds are environments such as games in which policies can compete, coopearate, and so on, then be judged by their performance. The profile works for a human User, acting as their Player; a semi-autonomous agent which develops and improves Policies for coworlds for and with their human User. Then a short vocabulary block from `vocabulary.md`, followed by a pointer to existing docs and files. 

The vocabulary's source, metta's `docs/ai/design/vocabulary.md`, is not on the host, so the block has to be copied into the profiles. It should be generated, the same way `tools/sync_shared.py` embeds the ontology into each SOUL today, so it can be refreshed from metta rather than edited by hand. Its core is the entity hierarchy (a User owns Players, a Player runs Policies, a Policy has PolicyVersions, and PolicyVersions compete in Episodes, which are grouped into Rounds within a Division of a League in a Coworld), plus the terms that are most often misused, such as Champion, which means an entry currently competing, not the winner.

Two terms clash with the profiles' own words and need to be resolved when the block is written. In Softmax, **promotion** is the commissioner moving an entry between Divisions, but the profiles' current ontology uses it for submitting a candidate as our champion; ours is now called submission (see Submission and Credits). And Softmax uses **Agent** as an alternative name for a Policy, which the block should note, since the profiles also call themselves agents.

The pointers should be the machine-readable surfaces Softmax already publishes for agents: `https://softmax.com/agents.md`, the guide for coding agents, which links the rest; `https://softmax.com/docs/llms.txt`, the documentation index; and `https://softmax.com/coworlds/llms.txt`, every public Coworld with its forum, wiki and participation guide.

### Profile's Role

More or less the information from the Agent Roles section, but aimed at the agent itself. Should be fairly concise, focus on the overall objectives and purpose of the profile and it's responsibilities, and briefly describe how to do them and the most common and relevant skills and tools, including a tool which lists all skills and tools the agent has available, including a complete list and is searchable.

Both roles are taught to treat the user's preferences as claims (see Claims). The Orchestrator, which is the one talking to the user, records them as they come up in conversation and keeps them current; both profiles read them, through the Session briefing (see Memory) and through claim search, and follow them in their work. This is also where each profile learns how it uses the work and knowledge representations, since that is where the two roles differ most: the Orchestrator creates Goals, Tasks and Experiments, accepts results and writes claims from what the user says, while the Researcher works one Task and its Experiment and leaves claims behind. The listing tool should build on what Hermes already has rather than start from scratch: `skills_list` and `skill_view` list and read skills, and `tool_search` searches the available tools.

### Policy Improvement Method

How to improve a policy well is knowledge both profiles need, now that the Orchestrator does work itself. It covers the habits that make improvement work trustworthy: diagnose from replays before changing strategy; change one thing per Experiment; know how noisy a metric is before trusting a difference in it; judge only by results from the live field, not local runs; and check refuted claims and earlier Experiments before trying an idea again.

The Researcher has most of this today, in its doctrine (`researcher/doctrine/`) and about a dozen skills (`experiment`, `candidate`, `ab-compare`, `calibrate-noise`, `replay-diagnosis`, `review-xp` and others), but it is written around the current ontology's hypothesis, experiment and result records, and the Orchestrator has none of it. It should be rewritten around Experiments and claims, and moved into skills both profiles share, with a few sentences in the role section pointing at them. Softmax's own description of this work, the optimizer and player roles in the `coworld` package's docs (`roles/OPTIMIZER.md` and `roles/PLAYER.md`), should be the starting point.

### Game Knowledge

Most of what makes an agent effective is specific to one game, and that knowledge should not live in the system prompt. It comes from three places: the Coworld's mixin (`games/<coworld>/`, with its `MIXIN.md` manifest and binding skills; see Skills and Tools); the league's published participation guide and Coworld manifest, which `game_support` fetches; and the Coworld's forum and wiki. The system prompt should say only where these are, and that they take precedence over general advice. Keeping game specifics out of the SOUL is what lets the same profiles serve any Coworld's workspace without their prompts slowly filling with the details of one game.

### CLIs and API

Short descriptions of each of the CLIs and their most common commands, then a pointer to a playbook of CLI commands. A mention that Softmax also provides an API which can be checked if the CLI commands don't do what the agent wants, and a pointer to the OpenAPI documentation the agent can look at if necessary. Also, a briefing on how to use the Player identity the workspace gets.

The playbook already exists upstream and should be used rather than written: `https://softmax.com/docs/skill.md` is Softmax's agent skill for the CLIs, request shapes and workflows, and `coworld docs --local` lists the reference docs installed with the CLI, including its `COOKBOOK.md` of workflow recipes. The local docs match the installed CLI version, which is a reason to keep the profiles' pinned versions current (today `coworld` 0.1.58 and `softmax-cli` 0.26.38, against 0.1.64 and 0.26.40 released). It also means the profiles should stop switching off the CLI's own agent guidance (`COWORLD_AGENT_GUIDANCE=0` in the wrapper), or replace it with an explicit pointer. The API documentation is at `https://softmax.com/api/observatory/docs`, with the OpenAPI contract at `https://softmax.com/api/observatory/openapi.json`; a direct call sends the Player's token as a bearer header.

The Player identity briefing is the one part only we can write, and the one most needed now that both profiles have a shell. It should cover how to run the real CLIs as the Player (today only the `"$RESEARCH_CLI" softmax` wrapper knows how to set up the credential); that the Player the profile acts as can differ from the Player the user is talking about; that the profile never switches identity; and that team-member access is not available as the Player. The design should also settle what the wrapper becomes: either it stays as the preferred path for the calls it covers, which carry receipts and checks a raw command doesn't, or it shrinks to setting up the identity and the agents use the real CLIs directly.

## Skills and Tools

The profiles' capabilities come in three layers. **Tools** do things: they read and write the representations, talk to the platform, and act on the IDE. **Core skills** teach the general method of improving a policy, and are the same for every Coworld. The **Coworld mixin** supplies what is specific to the one Coworld the workspace is for. Nothing in the profiles themselves should be specific to one game.

### Tools

Because the store is protected from the agents' general-purpose tools (see Storage), every operation on the representations is a Hermes plugin tool running in the Hermes process: creating, reading, searching and updating Goals, Tasks, claims and Experiments, inspecting evidence, proposing and making a submission, and reading credits. Today agents reach the records through the `research` CLI from their shell (`"$RESEARCH_CLI" records`, `kb` and `stats`), which can't work once the shell can't see the store. The `research` CLI shrinks to what works on the code repository and the platform: running the Softmax CLIs as the Player (`research softmax`), fetching sources, and building and checking policy code. The Orchestrator's current plugin tools, about 36 of them, are mostly about cards, receipts and the old submission flow; they should be rebuilt around the representations rather than patched, alongside the IDE's tools (see IDE Tools). Both profiles keep the `capabilities` tool, which lists and searches every tool and skill in the Session.

### Core skills

The core skills are the policy improvement method (see Policy Improvement Method). They ship with the profiles, are shared by both, and are written for any Coworld: wherever a step depends on the game, such as which metrics matter or how to read a replay, the skill looks the answer up in the mixin rather than assuming it. Today's 18 shared skills map onto the new design like this:

| Fate | Skills | Why |
|---|---|---|
| Rewrite around Experiments and claims | `experiment`, `xp-request`, `fetch-artifacts`, `review-xp`, `ab-compare`, `calibrate-noise`, `candidate`, `build-upload`, `diagnose`, `replay-diagnosis` | The core method, currently written around the old hypothesis, experiment and result records and the old submission rules |
| Rewrite to produce claims | `league-watch`, `meta-recon`, `opponent-profile` | Scouting stays useful, but its output becomes claims rather than hub pages |
| Merge into one `task` skill | `card`, `report` | Working a Task and finishing it with an outcome and result replaces the card handoff and the report |
| Remove | `kb-maintenance`, `lessons` | The claim tools and the merge wake keep the knowledge base sound, and lessons are just claims |
| Replace with workspace setup | `game-create` | A workspace is created for one Coworld, so its mixin is installed when the workspace is made, not by a card |

New skills cover what nothing teaches today: proposing a submission, reading and using credits, and, for the Orchestrator, the merge wake and working with the IDE.

### Coworld mixins

Each Coworld gets a **mixin**, in the shape established by `softmax-universe/optimizer-seed`: a folder of game-specific knowledge and tooling that plugs into the core skills. The workspace's code repository holds exactly one, vendored when the workspace is created, at `games/<coworld>/`. A mixin has:

* **`MIXIN.md`**, its manifest: the game, the Coworld and its version, the leagues, where the game's mechanics can be verified, the upstream mixin and commit it was vendored from, and a table of every skill the mixin provides.
* **Five required binding skills**, which the core skills resolve through that table: `ab` (the metrics that matter, how to split them, and the statistical tests and sample floors for comparing versions), `survey` (what a batch of episodes shows), `replay-inspection` (how to open and decode the game's replays), `eval-design` (rosters, episode counts and pacing for hosted evaluations), and `diagnosis` (the game's failure vocabulary and diagnostic instruments).
* **Optional extras**: strategy and meta documents, instruments and tools, and further skills, all listed in the manifest so they can be found.

A required binding that is missing or still a template stub is a **gap**. Gaps don't block work: the core skill says it has hit one, carries on with its general method, and the user is told that results will be weaker until the gap is filled. Filling a gap is ordinary work, a Task like any other, and its changes reach `main` through the merge wake. A Coworld with no published mixin starts from the template, with every binding a gap.

optimizer-seed's labs also carry their own memory files, such as working context, tentative lessons, closed levers and experiment logs. Those are not used here: their content belongs in the representations, as claims and Experiments in the store.

### Writing skills

Agents can write and improve skills as they learn. Skills in the Coworld mixin are part of the code repository, so an agent edits them like any other code, and the changes take effect for other Sessions once merged. The core skills ship with the profiles and are shared across every workspace, so changes to them are proposed rather than made: an agent writes its proposed change and says why, and a human decides whether it goes upstream. This replaces today's rule that every self-written skill waits in `skills-proposed/` for a human reply.

## Rollout

There is no migration. Existing workspaces are deleted rather than converted, and every user starts with a fresh workspace for each Coworld, set up with the new design from the start. This means:

* No converters for the current record types, no table of old ids, and no support for reading old-format workspaces. Compatibility code for earlier formats, such as the promotion-record migration (`shared/ide_research_core/legacy_promotions.py` and the gate's migration poll), can be removed rather than carried forward.
* Profiles are installed fresh, not updated, so no old `config.yaml`, cron job or SOUL survives.
* Platform state is unaffected: a user's Player, uploaded versions, XP request history and current champion stay on Softmax and are read live by the new workspace.
* Users lose their research history in the IDE, so they should be told before their workspaces are deleted.
