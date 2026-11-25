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

生产 MCP 的暂停和恢复还须经主机固定互锁文件
`/etc/dailycheck-application/routing-writes.lock`。该文件及路径各级目录由 root 持有，
文件权限为 `0644` 且只允许一个硬链接。写入事务从开始到提交持共享锁，且只接受文件
当前内容为 `enabled\n`。默认部署为 `disabled\n`。切换期间发布控制进程持同一 inode
的独占锁，并传给其固定 helper，直至 helper 正常退出、回切落点复核和候选下线完毕。

启用下列门禁需要 Peter 单独更新并核对 #52 relay 受保护安装目录中的控制源码及清单，
以及所有实际生产暂停写者的可执行版本、有效运行模式和互锁文件的设备号与 inode。
应用候选进目录时不会更新该发布控制面。先围栏所有早于互锁的暂停写者，确认没有存活
的进程或可重启入口。首次发布可从已核对的 v9 主库开始。relay 仅在受验证的旧基线
没有停订迁移、且仍指向蓝组的切流配置下接受 v9 停订表尚未创建。候选转正后，切流前
须在同一物理主库读取两张完整停订表并核对为空闲。兼容 Web 启动可以创建这两张表。
启用写入前须经已批准的正向迁移把实际主库登记为 v10，按既有 SQLite 备份流程留档。
预览库和应用目录中的库副本都不能作为依据。
受保护 relay 的 VPS profile 可显式设置 `routing_mode_proof: true`。仅支持停订的候选
在转正时使用此接线，并要求既有 Web 配置明确打开多仓模式。固定 root 私有
`/etc/dailycheck-relay/routing-proof.env` 仅容纳一行 routing proof 密钥，由 systemd
以服务凭据交给两条固定启动链。Web 接收密钥，MCP 同时接收由绿侧 Web 启动端口
确定的回环端口，两侧均显式打开多仓模式。检查转正落点时，relay 核对两个实际进程的
输入、在用主库和 Web 签名回执。预览继续使用独立副本和演练配置。
上线仍需核准实际实例配置及真实客户端验证，写围栏保持关闭直到另行批准。

旧 Web 可以提交仍处于暂停中的商品。发生回滚时，优先保留兼容 Web 承流。有暂停且
该 Web 无法服务时，由发布责任人关停门店下单入口并核实入口已停、旧运行态没有承流，
保留最终主库、购物车与审计，恢复一套有暂停保护且已核实主库和在服路由的 Web，再开放
入口。需要回到旧 Web 时，先依业务确认证据和现行权限恢复应解除的暂停。每次授权恢复
都通过原 MCP 操作留审计。严禁强制恢复、清表或以迁移前副本覆盖实际库。

核实只有支持互锁的生产写者可能接触该主库之后，发布责任人在固定文件上获取独占锁，
等待已接受的暂停写入事务退出，通过同一文件描述符原位写入 `disabled\n`、截到精确
9 字节并 fsync，再释放锁。禁止 unlink、rename 或使用会替换 inode 的编辑器。服务
命名空间中 `/proc/<pid>/root/etc/dailycheck-application/routing-writes.lock` 的
`stat -Lc '%d:%i'` 必须与控制面的设备号和 inode 一致。确认日常客户端的路由状态
读回 `writes_enabled=false` 并观察在途写者已被独占锁排空。随后可运行只读诊断：

```sh
python -m scripts.check_routing_web_rollback --db <当前在用的主库路径>
```

退出 1 表示有活动暂停，退出 2 表示无法确认。relay 在启动任何可能碰到切换的阶段前
自动复查受保护配置指定的主库身份、暂停状态与审计，并保持写入关闭。禁止用诊断
退出码代替 relay 守卫。回切因暂停或围栏拒绝时，状态记录 `routing_block`，保留原
尝试和锁。若是已验证尝试的签名 abort/reject，可用同一尝试重新取得原有远端事实并
重走原授权。若 staging 守卫阻断，签名重试只适用于已有观察回执的 abort/reject，
staging 没有自动重放入口。发布责任人维持原尝试和标记，先核对 Web/MCP 落点、进程
互斥锁和停止的 helper，再决定保留兼容侧或进入受控停止。切换执行后失败或结果不确定，
也继续用同一尝试对账。旧脚本
入口和人工 nginx/主库恢复必须停用或留在受控维护权限内。本机文件保持 disabled，
直到所有可承流 Web 均已确认保留暂停，且写入开放得到独立批准。
