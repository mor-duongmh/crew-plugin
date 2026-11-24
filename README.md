# crew-plugin

Plugin Claude Code cho máy dev trong một nhóm dùng crew-hub. Plugin ghép máy với hub bằng mã lấy từ Slack, cho xem và nhận task, mở worktree cho task. Các phiên Claude Code làm việc trong worktree đó sẽ hiện trên canvas của hub.

## Cần có

- Claude Code
- `python3` 3.9 trở lên (máy Mac có sẵn) và `git`
- Một crew-hub đang chạy, cùng workspace Slack của nhóm có lệnh `/crew`

## Cài

Trong Claude Code:

```
/plugin marketplace add mor-duongmh/crew-plugin
/plugin install crew@crew
```

Sau đó gõ `/crew pair` trên Slack. Lệnh trả về một dòng dạng `/crew:pair ABCD-EFGH http://...`; dán nguyên dòng đó vào Claude Code. Mã chỉ dùng được một lần và hết hạn sau 5 phút.

## Dùng

| Lệnh | Việc |
|---|---|
| `/crew:tasks` | Xem task: hàng chờ, đang làm, bị chặn |
| `/crew:start KEY` | Nhận task, tạo worktree `<repo>/.claude/worktrees/KEY` trên nhánh `crew/KEY`, rồi chuyển phiên vào đó |
| `/crew:release KEY` | Trả task về hàng chờ (worktree trên máy vẫn giữ) |
| `/crew:pair <mã> <hub>` | Ghép lại máy, ví dụ sau khi đổi hub |

Cũng có thể nói thẳng với Claude, ví dụ "lấy task T-3 về máy này"; Claude sẽ gọi tool MCP `crew` tương ứng.

## Gửi gì lên hub

- Chỉ sự kiện của các phiên có thư mục làm việc nằm trong một worktree crew. Việc ở mọi chỗ khác không rời máy.
- Chỉ các trường đã liệt kê sẵn: loại sự kiện, thời điểm, mã phiên, đường dẫn thư mục làm việc (trong worktree), mã, tên và model của agent, người nhận của `SendMessage`. Không bao giờ gửi prompt, mã nguồn, lệnh hay đầu ra của tool. Báo cáo của subagent chỉ gửi khi có dòng hợp đồng `crew-worker vN:`.
- Token của máy nằm ở `~/.crew/config.json` (quyền 600) và không bao giờ xuất hiện trong khung chat.

## Gỡ

```
/plugin uninstall crew@crew
```

Cấu hình và hàng đợi sự kiện nằm trong `~/.crew/`; xoá thư mục đó nếu không dùng nữa.
