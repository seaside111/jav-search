# 磁链复制兼容性验证（本地待发布）

测试日期：2026-09-07。对应 v1.4.6.25 的剪贴板兼容修复。

## 实测结果

| 浏览器 / 引擎 | 版本 | 结果 |
| --- | --- | --- |
| Chrome（本机安装版） | 152.0.7977.76 | 6/6 通过 |
| Edge（本机安装版） | 136.0.3240.64 | 6/6 通过 |
| Firefox（Playwright 测试版） | 146.0.1 | 6/6 通过 |
| Chromium（Playwright headless shell） | 145.0.7632.6 | 6/6 通过；默认原生写入被拒绝后走兼容复制 |
| WebKit（Windows 测试版，非 Safari） | 26.0 | 5/6 通过；原生异步复制后粘贴为空，未通过 |
| Safari（macOS / iOS） | — | 无真机环境，尚未验证 |

六个场景：正常接口、接口缺失、异步权限拒绝、延迟 100ms 后拒绝、兼容复制返回 false、兼容复制抛异常。

成功场景通过真实键盘粘贴比对完整内容，包含中文、空格、引号和超过 4KB 的链接。每次运行、每个场景的内容不同，避免旧剪贴板内容造成误判。失败场景验证手动复制弹窗包含完整磁链、未误报成功；同时检查临时文本框被移除、其他按钮不受影响、没有未捕获页面错误。

测试直接从 `frontend/index.html` 提取生产复制函数，在隔离浏览器上下文中点击测试按钮。未覆盖完整资源搜索流程、真实局域网 HTTP 部署或移动设备；HTTP 下接口缺失通过隐藏 Clipboard API 模拟。权限拒绝由测试夹具注入，未修改用户浏览器权限。

Windows WebKit 的兼容路径可真实复制粘贴；原生路径的失败不能据此判定 Safari 也失败。Playwright 官方测试记录 Windows WebKit 的剪贴板实现限制，参见 [permissions.spec.ts](https://github.com/microsoft/playwright/blob/main/tests/library/permissions.spec.ts)。Safari 仍需 macOS / iOS 真机复核，不将此项标为通过。

## 复跑

需要 Python Playwright 及对应测试浏览器，Chrome / Edge 使用本机安装版本。Firefox 在当前 Windows 沙箱内无法创建页面子进程，实测使用沙箱外的独立临时测试浏览器完成。

```powershell
python -m playwright install chromium firefox webkit
python frontend/tests/test_clipboard.py --browsers chrome edge chromium firefox
python frontend/tests/test_clipboard.py --browsers webkit --modes missing rejected delayed failed throws
```

不传参数会测试所有浏览器及全部场景，包括尚未通过的 Windows WebKit 原生场景；失败或缺少浏览器均返回非零退出码，不会静默跳过。

实现保持原生接口优先，并在用户点击触发的调用中立即开始写入；不可用或拒绝后使用选中文本复制，最终提供完整链接供手动复制。浏览器差异依据：[MDN Clipboard API](https://developer.mozilla.org/en-US/docs/Web/API/Clipboard_API)。
