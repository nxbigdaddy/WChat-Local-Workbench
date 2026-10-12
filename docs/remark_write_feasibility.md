# 微信 4.1.13.65 好友备注写入可行性

核验日期：2026-09-11。范围：公开项目文档、源码静态阅读；未操作微信界面、未导入运行第三方自动化库、未修改联系人或数据库。

## 后续本机复测：不能据此断言无法实现

2026-09-11 主代理再次唯一定位并激活现有微信窗口，只读刷新结果仍仅有根窗口和 `MMUIRenderSubWindowHW`，没有可定位的联系人或备注编辑框。内置截图调用再次返回 `SetIsBorderRequired failed: 不支持此接口 (0x80004002)`；未执行任何联系人编辑或保存操作。

本机 `sys.getwindowsversion().build` 实测为 **19045**。微软文档说明 `GraphicsCaptureSession.IsBorderRequired` 在 **10.0.20348.0** 引入。因此，当前捕获错误与该 API 不受本机系统支持吻合；这是依据系统版本、文档和错误名称的推断，未检查内置截图工具的内部实现。来源：[Microsoft IsBorderRequired API 要求](https://learn.microsoft.com/en-us/uwp/api/windows.graphics.capture.graphicscapturesession.isborderrequired?view=winrt-26100)。

本机已安装 Pillow 10.4.0 和 mss 10.1.0；它们可作为另行实现本地捕获的候选依赖，但本次没有调用这些库截图，也没有验证其输出能否识别微信资料和备注窗口。不能将“依赖已安装”计为“视觉自动修改已实现”。

更新结论：**目前只确认现有控件与内置截图路径受阻，没有证明 Python 自动修改备注在本机无法实现。** 下一项必要实测是替代捕获的画面质量与目标身份识别，再验证备注窗口填写、保存和独立读回。当前应用仍不支持自动写回。

## 结论

存在可研究的 Python UI 自动化接口：`Hello-Mr-Crab/pywechat` 项目中的 **pyweixin**，包括 `FriendSettings.change_remark(...)`。主源声明支持微信 **4.1.6+**，源代码包含 **4.1.9+** 的备注窗口分支，但没有本机 **4.1.13.65** 成功修改备注的实测证据。因此目前应标注为“源码确认存在，当前账号/UI 未验证”。

本机 UIA 此前只能读到 `MMUIRenderSubWindowHW`、没有业务控件，是主代理提供的既有观察，本调查没有重新读取桌面。若复测仍没有联系人/编辑框等控件，pyweixin 无法绕过这个条件；需要可用的本地视觉识别路径或由用户在微信中完成修改。截图的 `SetIsBorderRequired` 失败与 UIA 树缺失是两个不同问题，修复其中一个不能自动证明另一个已恢复。

不得通过修改解密副本或原始 `contact.db` 来声称微信备注已经更新、已同步。数据库读取通路适合用于修改前校验和修改后观察；真正写入需要触发微信自身的修改逻辑，可以通过界面，也可以研究经过版本适配的内部函数调用，不能把路径限定为截图。

## RPC / COM / HTTP 作为非截图路线

RPC 是客户端与执行后端之间的调用方式。建议结构是：Python 界面 → 仅本机 RPC → 经版本适配的微信修改逻辑 → 现有读取后端核验。现有通讯录和聊天读取可以封装为 RPC，但这不会自动赋予备注写能力。

- [Wechaty Contact API](https://wechaty.js.org/docs/api/contact/) 定义 `contact.alias(newAlias)`。其 [puppet-service 客户端](https://github.com/wechaty/puppet-service/blob/main/src/client/puppet-service.ts) 实际构造 `ContactAliasRequest` 并调用 gRPC `contactAlias`。这是接口与传输链存在的证据，最终是否能写由所选 Puppet 后端决定；未在用户账号登录验证任何服务。
- [ComWeChatRobot](https://github.com/ljc545w/ComWeChatRobot) 说明通过注入 DLL 调用微信内部函数，提供 COM/HTTP 封装并已添加修改备注功能。主分支对应微信 3.7.0.30，仓库 2026-04-08 归档，不能拿来当作当前 4.1.13.65 的现成适配。
- [wxhelper](https://github.com/ttttupup/wxhelper) 说明注入后启动 HTTP 服务，公开版本清单为 3.8/3.9 系列。仅证明“本地服务调用内部逻辑”的架构可行，尚未证明支持当前版本的好友备注写入。

结论：非截图 RPC 路线确实存在。当前缺少的是适配 4.1.13.65 且真正执行备注写入的后端证据，而不是 Python 无法调用 RPC。本轮只读检查文档和源码，没有注入、重装/降级微信、登录第三方协议服务或修改真实联系人。

## 主源与版本边界

| 候选 | 主源证据 | 本机结论 |
| --- | --- | --- |
| pyweixin（仓库名为 pywechat） | README 声明 4.1.6+、UI 自动化；备注方法对 4.1.9+ 使用顶层弹出窗 | 首选源码参考。尚未实测 4.1.13.65；需要账号确实暴露 UI 树 |
| cluic/wxauto4 | 主仓 README 只剩“停止更新” | 不作为当前 4.1.13.65 适配依据 |
| wxauto4 的现存公开 fork | 本次检查的 FreeWisdom/wxauto-4.0 README 仍声明仅 4.0.5 | 不应从“4.0”项目名称推断支持 4.1.13.65 |

来源：[pywechat README](https://github.com/Hello-Mr-Crab/pywechat/blob/ebe8d4be9347816d221c38843e5b9e5e480bd8fb/README.md)、[wxauto4 主仓](https://github.com/cluic/wxauto4)、[本次查看的 fork](https://github.com/FreeWisdom/wxauto-4.0)。

UI 树说明存在文档新旧冲突：仓库内旧 Skill API 参考建议开启讲述人；根目录当前 `Weixin4.0.md` 已明确说该方法失效，只列出现有 UI 树可见账号与 OCR 两条路径。不能向用户保证“打开讲述人就能恢复”；其中关于账号策略的解释也是项目作者观察，未经本机独立验证。来源：[当前 UI 可见性说明](https://github.com/Hello-Mr-Crab/pywechat/blob/ebe8d4be9347816d221c38843e5b9e5e480bd8fb/Weixin4.0.md)。

## 已核验源码

上游研究目标（**未随本仓库分发**，仅作只读参考）：`pywechat-research`，来源见 [THIRD_PARTY_NOTICES.md](../THIRD_PARTY_NOTICES.md)。

固定提交：`ebe8d4be9347816d221c38843e5b9e5e480bd8fb`。只进行了 clone 与文本阅读，没有安装依赖或导入库。

公开入口为 `from pyweixin import FriendSettings`，方法签名：

```python
FriendSettings.change_remark(
    friend: str,
    remark: str,
    description: str = None,
    phoneNum: str = None,
    clear_phoneNum: bool = False,
    search_pages: int = None,
    is_maximize: bool = None,
    close_weixin: bool = None,
)
```

该接口的 `friend` 是现有备注/显示名，不是稳定的微信内部用户名。研究到的执行路径为：

1. 打开该好友聊天的资料面板。
2. 点击“更多”，发送一次方向下键和回车。
3. 微信 4.1.9+：从桌面定位 `class_name='mmui::ProfileUniquePop'`、标题“设置备注和标签”的窗口；较旧版本从主窗口内定位。
4. 找到第 0 个 `Edit`，写入新备注，再点击“完成”。
5. 关闭聊天信息面板；依参数决定是否关闭微信窗口。

这是代码阅读结果，不代表该路径已经在当前微信运行成功。来源：[change_remark 源码，2105 行起](https://github.com/Hello-Mr-Crab/pywechat/blob/ebe8d4be9347816d221c38843e5b9e5e480bd8fb/src/pyweixin/WeChatAuto.py#L2105)、[窗口选择器，753 行](https://github.com/Hello-Mr-Crab/pywechat/blob/ebe8d4be9347816d221c38843e5b9e5e480bd8fb/src/pyweixin/Uielements.py#L753)。

上游代码不能原样作为本项目的批量写入实现，具体缺口如下：

- `get_search_result` 在多个相同显示名结果中取第一个；`search_friend` 还可能直接复用同标题的当前聊天。没有按目标内部用户名或可见微信号核对身份。
- 方法没有提交前旧值比较，也没有保存后的独立读取验证；返回 `None` 不能代表修改成功。
- 菜单导航依赖顺序，备注框依赖序号，窗口变化后需要先确认语义与状态。
- `Navigator.open_weixin` 会调整窗口大小、位置与置顶状态；不能将它作为不影响桌面的只读探针。
- 模块导入会设置 `pyautogui.FAILSAFE=False`。若采用上游库，需要独立的取消机制，不宜让 GUI 的停止按钮依赖这个失效保护。
- `close_weixin` 文档称默认关闭，但本提交 `GlobalConfig` 初值为 `False`。适配层应显式传值，不能依赖不一致的文档默认值。

来源：[搜索取首项，521 行起](https://github.com/Hello-Mr-Crab/pywechat/blob/ebe8d4be9347816d221c38843e5b9e5e480bd8fb/src/pyweixin/WeChatTools.py#L521)、[打开微信，753 行起](https://github.com/Hello-Mr-Crab/pywechat/blob/ebe8d4be9347816d221c38843e5b9e5e480bd8fb/src/pyweixin/WeChatTools.py#L753)、[模块初始化，89 行起](https://github.com/Hello-Mr-Crab/pywechat/blob/ebe8d4be9347816d221c38843e5b9e5e480bd8fb/src/pyweixin/WeChatAuto.py#L89)、[实际配置初值，49 行起](https://github.com/Hello-Mr-Crab/pywechat/blob/ebe8d4be9347816d221c38843e5b9e5e480bd8fb/src/pyweixin/Config.py#L49)。

## 建议实现的最小适配接口

以下是本项目的设计建议，尚未实现或实测。可以直接用已安装的 `uiautomation` 构建窄功能适配层，参考上游的窗口结构；无需把整个带收发消息能力的库引入默认启动路径。

```python
probe() -> Capability
# 只读：微信进程/版本、已登录主窗、必要业务控件是否存在。

prepare(contact_id, expected_old_remark, new_remark) -> PreparedEdit
# 在 UI 中定位联系人，读取身份与旧备注，形成待提交操作。

apply(prepared_edit) -> WriteResult
# 重验目标与旧值；编辑指定备注框，读回，再点击完成。

verify(contact_id, expected_new_remark) -> VerificationResult
# 重新观察资料面板，并独立刷新本项目的本地数据库读取结果。

cancel(prepared_edit) -> None
# 取消当前尚未提交的备注编辑，不发送消息。
```

适配层应满足以下约束：

1. **身份检查**：GUI 以现有读取层的稳定 `contact_id/username` 选中目标。自动化进入资料后，将可见“微信号”与该记录的 `alias` 或确实显示出来的内部用户名比对。显示名只用于搜索。重名不取首项；无法验证稳定身份时转人工选择，并明确状态。
2. **提交检查**：保存 `expected_old_remark`，在填写前和提交前比较；旧值发生变化即停止。备注为空视为明确的清空操作，不把空字符串当作漏传参数。
3. **界面检查**：限定进程、窗口、编辑框角色及对话框标题。定位失败时返回能力不足，不发送盲目的回车/点击。修改前不关闭微信、不执行网络查找好友回退。
4. **字段边界**：仅写备注，保留电话、描述和标签。填写后先读取编辑框确认，之后才提交。每次只处理一个联系人。
5. **结果检查**：至少区分 `not_started`、`prepared`、`submitted`、`client_verified`、`source_verified`、`failed`、`unknown`。保存动作后异常且结果未知时先观察，不自动重放。
6. **独立验证**：资料面板重新读取成功后可标记 `client_verified`；本地数据库/WAL 刷新读到同一联系人新备注后再标记 `source_verified`。这仍然不是腾讯服务器或手机端同步的独立证明。
7. **交互隔离**：单一 UI worker 串行执行；提供立即取消与有限超时。由用户在本工具中选择联系人、填写新值并点击应用即可授权该次操作，不需要额外多层确认。

## 下一步最小验收

| 检查 | 当前状态 | 通过条件 |
| --- | --- | --- |
| 上游存在 4.1+ 备注方法 | passed（静态） | 本文固定提交中有上述源码 |
| 本机业务 UI 树可用 | 未由本调查重测；主代理既有观察为不可见 | 能读取主窗、好友身份、备注对话框必要控件 |
| 本机 4.1.13.65 打开备注窗口 | not_run | 唯一联系人身份核对成功，正确读取旧值 |
| 改名与恢复 | not_run | 在用户指定测试联系人上修改，再恢复，均独立读回 |
| 重名保护 | not_run | 两个同名目标时不发生首项误选 |
| 旧值冲突/中途取消/提交后异常 | not_run | 停止或保留未知状态，无自动二次提交 |

如果 UI 树仍不可见，GUI 可以继续提供通讯录/聊天查看、备注草稿、复制新备注和人工完成后的刷新验证；应明确标注“未写入微信”。自动 OCR 写入需另行验证本地捕获与身份识别，不应在无法观察当前界面的条件下用固定坐标冒充完成。
