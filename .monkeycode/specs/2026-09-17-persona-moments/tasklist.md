# 分身朋友圈实施计划

- [x] 1. 数据层：朋友圈表与访问接口（R1-R3、R8、R11-R12、R20-R21）
  - [x] 1.1 在 store.py 的 SCHEMA 中新增 moments、moment_likes、moment_comments 三张表与索引（含 UNIQUE(moment_id, user_id)）
  - [x] 1.2 实现动态读写：add_moment、get_moment、list_moments（倒序游标分页并聚合 like_count/comment_count/liked）、delete_moment（级联删除点赞与评论）、count_moments_on、last_moment_at
  - [x] 1.3 实现互动读写：set_moment_like（幂等增删）、list_moment_comments、add_moment_comment、set_moment_comment_reply
  - [x] 1.4 在 _PERSONA_CHILD_TABLES 加入 moments，并在 delete_persona 与 purge_user_content 中先级联清理对应点赞/评论
  - [x] 1.5 单元测试：跨用户隔离、游标分页、点赞幂等、删除级联、当日计数（覆盖属性：归属不变式、点赞幂等、级联不变式）
  - [x] 1.6 属性测试：任意动态被删除后其点赞与评论无残留（级联不变式，R20）

- [x] 2. 设置项：朋友圈配置（R17-R19）
  - [x] 2.1 persona_settings.DEFAULT_SETTINGS 新增 moments 段（enabled=false、max_per_day=2、min_gap_minutes=180、prompt=""），并在 validate 中收敛取值范围
  - [x] 2.2 单元测试 validate 对 moments 段的取值收敛（R17）

- [x] 3. 检查点 - 确保所有测试通过
  - 运行 python3 -m unittest discover -s tests，如有疑问请询问用户

- [x] 4. 内容生成（R4-R7、R16）
  - [x] 4.1 agent.py 抽出 _generate_and_clean()（复用乱码重采与提示词泄漏过滤），新增 compose_moment()，系统提示为人设 + 记忆背景 + MOMENT_STYLE_RULES，不含聊天记录
  - [x] 4.2 新增 moments.py：MOMENT_PROMPT、MOMENT_STYLE_RULES、build_prompt(settings, memories)、compose(agent, settings, memories)（复用 media_reply 从 store.list_stickers 选配图）
  - [x] 4.3 单元测试 compose：伪造 Agent 验证泄漏与乱码过滤、配图挑选（防泄漏属性，R5）

- [x] 5. 调度器（R18-R19、R22-R24）
  - [x] 5.1 实现 moments.MomentScheduler：轮询 ready 且开启朋友圈的人格，判断时间窗（复用 proactive 窗口）、每日上限、最小间隔与尝试冷却；扣费成功后再生成，生成或落库失败退款
  - [x] 5.2 积分不足分支：跳过本次并退款，按 (persona_id, 当天) 去重提醒用户一次；平台模型未就绪时静默跳过
  - [x] 5.3 webapp 新增 get_moments_scheduler()，在 startup 启动、shutdown 停止
  - [x] 5.4 单元测试 tick：关闭/窗口外/达上限跳过、失败退款不落库、成功恰好扣费一次（计费不变式，R22/R24）、积分不足提醒每日一次（R23）

- [x] 6. 检查点 - 确保所有测试通过
  - 运行 python3 -m unittest discover -s tests 与 ruff check ex_persona tests，如有疑问请询问用户

- [x] 7. API：朋友圈接口（R8-R16、R20）
  - [x] 7.1 GET /api/personas/{persona_id}/moments 列表（before_id/limit）与 GET /app/moments 页面路由
  - [x] 7.2 POST /api/personas/{persona_id}/moments 手动发布（校验归属与上限；扣费后生成，失败退款）与 DELETE /api/moments/{moment_id}
  - [x] 7.3 POST/DELETE /api/moments/{moment_id}/like 幂等点赞与取消
  - [x] 7.4 POST /api/moments/{moment_id}/comments 评论（扣费，积分不足拒绝且不落库）并生成分身回应；GET 评论列表；POST /api/moments/comments/{comment_id}/retry 重试失败回应
  - [x] 7.5 集成测试：跨用户访问 404、手动发布扣费、评论扣费与回应、积分不足拒绝评论、重试接口（R3/R13/R15/R16）

- [x] 8. 前端：朋友圈页面与入口
  - [x] 8.1 新增 web/moments.html：分身切换、动态卡片（正文/配图/时间/点赞数/评论数）、点赞、评论、删除、空状态、手动「让 ta 发一条」；页内命名空间样式，无 emoji
  - [x] 8.2 app.js 新增 moments 图标并加入 NAV_ITEMS 第 5 项；app.css 适配 5 项 tabbar 的窄屏间距
  - [x] 8.3 agent.html 人格设置增加朋友圈开关、每日上限、最小间隔、生成提示词并接入保存（设计稿要求写入人格设置）
  - [x] 8.4 前端静态检查：抽取内联脚本并通过 node --check

- [x] 9. 端到端与全量回归
  - [x] 9.1 scripts/e2e/run_chain.py 增加「发布 -> 列表 -> 点赞 -> 评论 -> 回应 -> 删除」链路
  - [x] 9.2 全量回归：python3 -m unittest discover -s tests、ruff check ex_persona tests、python3 scripts/e2e/run_chain.py

- [x] 10. 最终检查点 - 确保所有测试通过
  - [x] 确认全部单测（304）与 e2e（54/54）通过，ruff 无告警；已部署 `20260917_114341` 并线上验证发布/点赞/评论回应/删除与前端页面
