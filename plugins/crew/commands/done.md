---
description: Đánh dấu task crew mình đang làm là xong
argument-hint: KEY [ghi chú]
---
Gọi tool `crew_done` của MCP server `crew`. Từ `$ARGUMENTS`: từ đầu tiên là `key`, phần còn lại (nếu có) là `note`. Thiếu mã task thì không gọi tool, chỉ nhắc người dùng gõ `/crew:done KEY`. Báo lại kết quả của tool một câu.
