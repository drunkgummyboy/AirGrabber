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
