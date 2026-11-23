# Gene 单品缺货停订

## 权限及日常操作

给客户端单独配置 StreamableHTTP `/api/mcp/` 的精确策略凭据。原只读连接可以继续使用。
客户端以已批准的 `canonical_id` 和 `warehouse_code` 调用 `routing_item_status`。返回范围
由权限预设决定，包含主品项标识、名称、状态、全局 SKU、基础单位与换算，授权仓的
SKU、单位和绑定状态，以及本仓有效来源、停订原因和版本。查询的可订性基于原仓、
主品项和仓内绑定，门店专属和门店停用规则会进一步限制实际目录。
`existing_open_orders` 是该品项与来源仓仍在待审、已审或已发货的历史订单数，供跟进。

授权业务负责人核实原仓的可分配量，记录可追溯的仓内人工核实或调度确认编号，随后：

1. 原仓确认无可分配量，用 `routing_pause_item` 和查询返回的 `version`。成功回执
   包含持久化 `pause_id`、新版、原仓和操作证据。
2. 有效暂停期间，目录和加购均拦截该品项。旧购物车给出品名。门店移除对应行后
   可以按原流程提交余下品项。历史订单仍使用已固化的来源和原先履约流程。
3. 补货后取得一次新的确认。以 `routing_restore_item` 提交同一品项、同一原仓、
   当前版本、当前暂停记录和新证据。

写入使用请求内的 `expected_version`、本令牌唯一的 `idempotency_key` 和 `evidence`。
证据结构包含 `canonical_id`、`warehouse_code`、`kind`、`reference`、带时区的
`observed_at`、十进制字符串 `quantity` 和基础单位 `unit`。恢复时请求和证据都
须带相同 `pause_id`。可接受的类型是 `manual_fulfillment_confirmation`。暂停要求量为
0，恢复要求暂停后确认的正数，两个动作各自引用一份核实记录。例如在合成环境查询
201 / dc_alpha，暂停时填写：

```json
{
  "canonical_id": 201,
  "warehouse_code": "dc_alpha",
  "expected_version": "<当前查询返回的 version>",
  "idempotency_key": "fixture-pause-001",
  "evidence": {
    "canonical_id": 201,
    "warehouse_code": "dc_alpha",
    "kind": "manual_fulfillment_confirmation",
    "reference": "SYNTHETIC-CHECK-001",
    "observed_at": "<当次核实 UTC ISO 8601 时间>",
    "quantity": "0",
    "unit": "件"
  }
}
```

在授权与模式有效时，原样重试返回原回执。同键改内容或使用旧版本返回冲突。发生
冲突后重读状态并按最新业务事实重新决定。恢复前只有账面量时保留停订待核。系统
校验证据结构、绑定、时间、单位、数量方向和重复引用，确认实际可分配量及外部记录
真实性由授权业务负责人承担。门店提交不预占库存。

## 在本机用日常客户端复跑

使用仓内依赖环境。从本工作树启动一次性实验室：

```sh
python -m tests.test_routing_stockout_http
```

使用终端显示的回环 URL 和当次合成 Bearer 新建连接，保留日常的只读连接。实验室退出
即销毁合成库和凭据。将另一个窗口打开合成 Web 目录，用终端显示的门店账号登录。
对品项 201 执行查询、预先加车、暂停、用旧购物车提交、移除后提交余下 206，再以新
确认恢复。客户端直接复制提交相同工具参数可复核幂等，同键修改内容可复核冲突。
各脚本调用、明确负例和 SQLite 断言可通过以下命令自动执行：

```sh
task_tmp="$(mktemp -d)"
mkdir -p "$task_tmp/db"
TMPDIR="$task_tmp" DAILYCHECK_DB_DIR="$task_tmp/db" \
  python -m pytest -q tests/test_routing_stockout_http.py
```

## 部署和回退边界

v10 走已注册的 master 迁移及 SQLite 备份，仓库库不需要新迁移。早期草稿的 v10 合成库
可通过本版 Web 初始化补齐追加审计表。普通 Web Token 管理页
继续使用原有的门店只读授权。生产主体、具体配对和业务决定来源由 Peter 核定。

负责登记的管理员在受控主机使用以下同一入口完成三种决定的只读预览。每次均提交
`--issuer`（核准的管理员 ID）和独立的 `--reference`（配置决定或已批准业务决定的
可追溯编号），并交互验证管理员本人密码：

- `python -m scripts.routing_policy_token --db <当前主库> authorization-preview --operation install-preset`
  登记固定的 `dailycheck.gene-routing` 预设和版本。预设只有已发布的停订工具动作，独立于
  主体、配对和令牌。
- 同一命令使用 `--operation approve --actor <Gene ID> --pair <canonical_id> <原仓码>`，
  为单一活跃 Gene 主体登记一对有效来源。确认第一对时可登记该主体，之后新增配对
  保留同一主体的既有批准。确认复核主品项与主体的租户、原仓、绑定资格、路由版本
  和这对配对的审批历史版本。其他操作批准并撤销后，较早的未用预览即失效。
- 同一命令使用 `--operation revoke` 和同样精确的主体及配对，撤销当前审批代次。撤销
  不移除原有暂停，Gene 需要新的业务批准及有效凭据才能通过 MCP 执行恢复。

执行同一命令的 `authorization-confirm --issuer <同一管理员> --confirmation <预览确认值>`
提交明示确认，预览结果和已消耗的确认均无法覆盖既有历史。主库保存登记操作者、UTC 时间、
来源引用和对应的预设版本或审批代次。引用真实性由负责登记的管理员依据已批准的独立业务
记录核对。重新批准同一对需要新的决定引用和新的代次。

批准后使用 `python -m scripts.routing_policy_token --db <当前主库> preview`，带
`--issuer`（核准的发放人 ID）、`--actor`（Gene ID）、`--name`、`--preset`、
`--version` 和逐对重复的 `--pair <canonical_id> <原仓码>`，交互输入发放人本人密码。
预览只读，绑定发放人、数据库文件、预设和配对审批记录。仅受控主机帐号可操作该主库，
应用管理员身份还须通过密码验证。签名确认使用独立的
`DAILYCHECK_ROUTING_ISSUANCE_SECRET`，保留在受控的本机环境。

输入预览中的确认值，通过同一命令的 `issue --issuer <同一发放人> --confirmation <确认值>
--token-file <私有目录内的新文件>` 确认。签发重新核对密码、源仓和授权，已有输出文件
会使整个事务拒绝。操作系统账号持有输出目录且目录仅其可访问。stdout 仅返回 token ID，
凭据写入独占创建的 `0600` 文件供取用。Token 管理页继续使用原撤销入口，在同一事务
撤销令牌和策略绑定。撤销配对记录会立即停用依赖它的令牌及旧预览。重新批准同一配对
需要新审批记录和新令牌，旧代次保留且无法复活。


MCP 写入要求自身开启多仓模式，并向指定的同机 Web 回环端口核实模式、主库文件和
演练状态。需要两进程设置相同的独立随机 `DAILYCHECK_ROUTING_PROBE_SECRET`
（至少 32 字符），以及在 MCP 指定 `DAILYCHECK_ROUTING_WEB_PORT`。在生产候选中
须核实每个接流量的 Web 实例与 MCP 的实际有效模式和共享主库，签名挑战的结果只代表
配置指向的 Web 实例。Web 会话密钥和 MCP Bearer 不参与此核验。

停用 MCP 写入能力时保留支持暂停的 Web 版本，仍有暂停的品项会继续受保护。旧 Web
版本不识别 v10 暂停，因此回退前要先停止接受新的停订写入，核对全部现存暂停。若仍
需保持停订，继续由具备 v10 暂停保护的 Web 承接门店流量。仅在全部暂停已通过经授权
的业务恢复结案，且下列只读检查退出 0 时，才能在写入保持隔离的条件下切回旧 Web：

```sh
python -m scripts.check_routing_web_rollback --db <当前在用的主库路径>
```

退出 1 表示有活动暂停，退出 2 表示无法确认。v10 schema down 主动拒绝以保存审计。
迁移失败由事务回滚。迁移前备份用于尚未开放写入的恢复，开放后保留现有主库与事件，
避免用旧备份覆盖期间的订单与停订事实。
