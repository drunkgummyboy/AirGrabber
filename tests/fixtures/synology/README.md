DSM Web API answers for Download Station, constructed from Synology's "Download Station Web API"
guide and DSM 7's DownloadStation2 web client (no live NAS was called; addresses and names are
made up):

- `api_info.json`: `SYNO.API.Info` `query` on a DSM 7 box (both SYNO.DownloadStation.Task and
  SYNO.DownloadStation2.Task offered)
- `task_list.json`: `SYNO.DownloadStation.Task` `list` with `additional=detail,transfer` (guide,
  section "list": `tasks` with `title`, `size`, `status` waiting / downloading / paused / finishing
  / finished / hash_checking / seeding / filehosting_waiting / extracting / error, `status_extra`,
  and `additional.detail.uri` (a magnet, or the uploaded .torrent's file name) and
  `additional.transfer.size_downloaded`). Sizes appear as strings, as in the guide's examples.
- `task2_list.json`: `SYNO.DownloadStation2.Task` `list`, as DSM 7's web client receives it: the
  list under `task`, numeric sizes and numbered states (2 = downloading; 100 and up = an error;
  other numbers are read by their progress, because Synology doesn't document them). The magnet
  carries its info hash in base32.
- `task_action.json`: `SYNO.DownloadStation.Task` `pause` / `resume` / `delete` (guide: a list with
  the `id` and an `error` code per task, 0 = done; the ids go in as `id=dbid_1,dbid_2`, a delete
  with `force_complete=false`)
- `task_action_refused.json`: the same with error 405 ("invalid task action") for one task; 404
  ("invalid task id") is a task that is gone
- `task2_action.json`: `SYNO.DownloadStation2.Task` `pause` / `resume` / `delete` with the ids as a
  JSON list, assumed to answer like DSM 7's web client sees it (`failed_task`, empty when all went
  well; not in Synology's guide)
