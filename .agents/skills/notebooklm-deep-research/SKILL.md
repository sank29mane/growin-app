---
name: notebooklm-deep-research
description: Perform deep research using NotebookLM to gather comprehensive information from the web or Drive. Use for "deep research on X", "find sources about Y", or gathering detailed data.
---

# NotebookLM Deep Research

## Overview

This skill leverages NotebookLM's "Deep Research" capabilities to autonomously find, analyze, and synthesize information from the web or Google Drive. It is ideal for complex topics requiring broad coverage, high-quality sourcing, and synthesized answers.

## Workflow

Follow this sequence to perform deep research:

### 1. Initiate Research

Use `research_start` to begin the research process.

- **Query**: Be specific (e.g., "latest advances in solid state batteries 2024-2025" instead of "batteries").
- **Mode**:
    - `deep`: (~5 mins, ~40 sources). Best for comprehensive reports.
    - `fast`: (~30s, ~10 sources). Best for quick fact-checking.
- **Source**: `web` (default) or `drive`.

```javascript
// Example
await research_start({
  query: "impact of AI on software engineering jobs 2025",
  mode: "deep",
  notebook_id: "optional-existing-notebook-id" // Omit to create new
});
```

### 2. Monitor Progress

The research process is asynchronous. You MUST poll for completion using `research_status`.

- **Loop**: Call `research_status` every 30-60 seconds.
- **Wait**: Continue until `status` is "completed".
- **Timeout**: Respect the `max_wait` parameter or your own internal timeout.

```javascript
// Example polling loop
let status = await research_status({ notebook_id: "..." });
while (status.status !== "completed") {
  // Wait...
  status = await research_status({ notebook_id: "..." });
}
```

### 3. Import Sources

Once status is "completed", you must explicitly import the findings into the notebook.

```javascript
await research_import({
  notebook_id: "...",
  task_id: "task-id-from-status-response"
});
```

### 4. Synthesize & Answer

After import, the sources are available in the notebook. Use standard NotebookLM tools to answer the user's question.

- **`notebook_query`**: Ask specific questions based on the new sources.
- **`audio_overview_create`**: Generate a podcast-style summary.
- **`report_create`**: Generate a briefing doc or study guide.

## Best Practices

- **New Notebooks**: For a fresh research topic, allow `research_start` to create a new notebook (leave `notebook_id` null). This keeps context clean.
- **Existing Notebooks**: If researching a sub-topic for an existing project, pass the `notebook_id`.
- **Transparency**: Tell the user you are starting a "Deep Research" task which may take a few minutes.
- **Error Handling**: If `research_status` returns "failed", report the error to the user and offer to try "fast" mode or a manual search.
