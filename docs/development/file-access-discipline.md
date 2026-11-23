# Agent file access

Cosir requires the model to see the current contents of an existing file before editing it. A complete replacement requires the entire current file. A successful controlled write of model-supplied content also establishes knowledge of that version. A new file has no contents to read.

File reads and confirmed edits travel in the structured facts attached to results. The next model request can use those facts while their source messages remain in context. A batch of calls from one model response cannot use a read it performs in that batch to authorize an edit. A new version created by a confirmed file tool can carry forward knowledge already available to the batch. File identity and metadata are checked before writes. These checks address ordinary mistakes and concurrent changes in the harness.

## Terminal file changes

Agent commands may use regular shells and scripts to edit files. Cosir checks any write targets it can recognize from literal shell syntax before launching the command, including recognizable parts of a compound command. Commands with effects that Cosir cannot infer still run and are covered by the workspace comparison.

Recognized writes apply the same rules as file tools. Read an existing file before editing or deleting it, and read the full file before replacing it. For a recognized move, read the source and read any existing destination in full.

Before choosing a write action, obtain the source contents using a file tool or a recognizable terminal read. Common literal reads include POSIX `cat`, `head`, `tail`, `sed -n`, and numbered `rg`/`grep`, PowerShell `Get-Content`, and cmd `type`. Cosir validates retained terminal output against the source version. A script's internal reads, a no-match result, and the user's terminal transcript do not grant edit qualification.

## Observed changes

Cosir compares a bounded inventory of filesystem versions at command start, while long commands run, and after the worker returns. This also works for non-Git workspaces with the file panel closed. Background and foreground work share the same comparison. A command stopped or timed out can already have changed a file. Unavailable or interrupted comparisons are marked incomplete. Changes during a command window can also come from the user or another process.

The comparison excludes application and Git metadata, common dependency/cache directories, and explicitly configured generated directories. Put an array of workspace-relative directory paths in `.cosir/generated-directories.json` for additional exclusions, for example `["dist", "build/generated"]`. Recognized targets and previously observed files remain subject to direct version checks. Generated directories outside the workspace cannot be configured. Search ignores and UI visibility do not affect the audit scope.

The report distinguishes targets recognized before launch from other observed effects. A script's indirect write can occur before it is reported. Comparisons report net changes and can miss intermediate writes restored to the original version.
