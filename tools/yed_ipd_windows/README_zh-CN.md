# YED IPD Windows 测试工具

该工具通过开发板的 AT 串口完成 SoftAP TCP Server 多连接回环测试。TCP Client 由客户自行准备。

## 固定测试拓扑

- 开发板模式：SoftAP
- SoftAP IP：`192.168.4.1`
- TCP Server：`192.168.4.1:4000`
- 最大 TCP 连接数：5
- 有效测试要求：至少 2 个 link 收到数据
- AT/CLI 波特率：115200
- 默认 SSID：`YED_IPD_TEST`
- 默认密码：`12345678`

客户端连接 SoftAP 后，连接 `192.168.4.1:4000` 并持续发送数据。工具收到每个可靠 IPD 帧后执行：

1. 校验 IPD 头部 CRC。
2. 发送 `AT+IPDACK=<frame_id>`。
3. 等待 ACK 命令返回 `OK`。
4. 发送 `AT+CIPSEND=<linkid>,<len>`。
5. 将收到的数据原样回发给相同 TCP Client。
6. 等待 `SEND OK`，然后处理下一帧。

测试时长从收到第一帧 TCP 数据开始计算。未收到数据时工具保持等待，不消耗测试时长。

## 重传故障注入

界面的 `Fault interval (frames)` 用于配置重传验证间隔，默认值为 `100`，填写 `0` 可关闭故障注入。包数按所有 TCP link 收到的唯一 IPD 帧合计，不是每条 link 分别计数。

默认配置下，工具按以下方式交替验证两种重传路径：

1. 第 100 包不发 ACK，发送 `AT+IPDRETRY=<frame_id>`，明确要求设备重传。
2. 第 200 包既不发 ACK，也不发送 `AT+IPDRETRY`，等待设备在 ACK 超时后主动重传。
3. 第 300 包再次明确要求重传，第 400 包再次等待超时重传，后续持续交替。

收到预期重传后，工具会核对 `frame_id`、link 和 payload 是否与原包完全一致。校验通过后才发送 ACK，并把该包原样回发给对应 TCP Client。重传内容不一致、未收到重传、出现额外重复包，或重传后的 ACK/回发失败，测试均判定为 `FAIL`。

`AT+IPDACK` 首次无回复或返回 `ERROR` 时，工具会自动重发一次，以便继续收集长时间压测日志。发生过 ACK 命令重试的测试最终仍判定为 `FAIL`，并在报告中记录对应 link 和次数。

## 使用 EXE

1. 连接开发板 AT 串口；需要固件诊断日志时再连接 CLI 串口。
2. 运行 `YED_IPD_Test_YYYYMMDD_HHMMSS.exe`。
3. 选择 AT port 和可选的 CLI port。
4. 设置 SSID、密码、信道、测试时长和故障注入间隔，点击 `Start`。
5. TCP Client 连接上述 SoftAP 和 Server，发送数据并校验回包与发送内容相同。
6. 测试完成后点击 `Open logs` 获取报告。

CLI port 被选择时，工具启动后自动执行 `yed_ipd_debug 1`，退出时自动执行 `yed_ipd_debug 0`。CLI 日志不是测试运行的必要条件，但客户问题定位建议连接。

工具启动时会验证 AT 串口响应。如果 AT/CLI 两个所选 COM 口顺序相反，工具会尝试自动交换并在事件日志中记录最终端口。CLI 调试命令只负责下发，不要求识别回复；CLI 串口输出会持续原样保存。打开串口导致开发板短暂复位时，工具会在限定时间内重试 AT 握手。

## 报告文件

每次运行创建独立的 `YED_IPD_YYYYMMDD_HHMMSS` 目录：

默认在 `YED_IPD_Test_YYYYMMDD_HHMMSS.exe` 所在目录中创建测试目录，也可以在启动测试前通过 `Browse` 修改输出位置。

- `summary.txt`：客户可直接查看的最终结论和各 link 统计。
- `summary.json`：结构化统计。
- `events.log`：连接、ACK、echo 和异常时序。
- `at_uart.bin`：完整 AT 串口原始数据，包含 TCP payload。
- `cli_uart.log`：带时间戳的 CLI 串口日志。
- `config.json`：本次测试参数。

`PASS` 表示至少两个 link 收到数据、到达设定时长，所有计划内重传均正确完成，并且没有计划外重传、drop、CRC、AT 命令或发送异常；`FAIL` 表示上述任一条件不满足；`STOPPED` 表示人工停止。

## Windows 打包

安装 Python 3.10 或更新版本后，双击 `build_windows.bat`。脚本将在 Windows 本机生成：

```text
dist\YED_IPD_Test_YYYYMMDD_HHMMSS.exe
```

文件名后缀是构建时间，例如 `YED_IPD_Test_20260828_103015.exe`。

也可以双击 `run_windows.bat` 直接以 Python 方式启动。
