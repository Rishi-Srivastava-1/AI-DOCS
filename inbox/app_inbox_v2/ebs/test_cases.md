# App Inbox V2 — FStore Batch Task Recovery Test Cases

| # | Scenario | Expected Result | EBS Required | Tested |
|---|---|---|---|---|
| 1 | LP graceful stop — items in `persistBatchTask` memory, before fileQueue write | No data loss. `shutdown()` flushes in-memory items to fileQueue, then drains to FStore | No | |
| 2 | LP OOM — items in `persistBatchTask` memory, before fileQueue write | Data lost. In-memory queue gone, never reached disk | No | |
| 3 | LP graceful stop — items in fileQueue on disk | No data loss. `shutdown()` drains fileQueue to FStore before exit | No | |
| 4 | LP OOM — items in fileQueue on disk | Data lost. New container starts with fresh overlay filesystem, files gone | No | |
| 5 | LP OOM — items in fileQueue on disk + EBS mounted | No data loss. Files survive on EBS, new container picks them up on next drain | Yes | |
| 6 | LP graceful stop — drain mid-execution, before `fileQueue.dequeue()` | No data loss. Item still on disk, replayed on restart. `batchDelete` called twice but idempotent | No | |
| 7 | LP OOM — drain mid-execution, before `fileQueue.dequeue()` | No data loss. Item still on disk (not yet dequeued), replayed on next drain | No | |
| 8 | LP graceful stop — drain mid-execution, after `fileQueue.dequeue()` | No data loss. `fstoreBatchTask.shutdown()` waits up to 2 minutes for FStore call to finish | No | |
| 9 | LP OOM — drain mid-execution, after `fileQueue.dequeue()` | Data lost. Item removed from disk, FStore call never completed, in-memory item gone | No | |
