---
description: Nhận task crew về máy này, mở worktree của nó và chuyển phiên vào đó
argument-hint: KEY
---
Gọi tool `crew_start` của MCP server `crew` với `key` là `$ARGUMENTS` và `repo` là đường dẫn tuyệt đối của repo git đang mở trong phiên này.

Nếu tool tạo được worktree, dùng Bash chạy đúng một lệnh `cd <đường dẫn worktree>` để phiên này làm việc trong worktree: chỉ việc làm trong worktree mới hiện trên canvas. Báo lại ngắn gọn: task đã nhận chưa, đường dẫn worktree, tên nhánh, và rằng phiên đã chuyển vào worktree.
