"""在 Dify 的 api 容器里执行：备份现有应用 DSL，或把新 DSL 覆盖导入到同一个应用。

为什么要在容器里跑：
  控制台导入接口要已登录的会话（cookie + CSRF），浏览器自动化在这台机器上没启用。
  容器里可以直接调用 Dify 自己的控制台服务，用同一个代码路径完成导入，不需要账号密码。

用法（宿主机，需要 Docker 权限）：
    docker cp dify-ai-news-v1.yml docker-api-1:/tmp/dify-ai-news-v1.yml
    docker cp tools/dify_container_import.py docker-api-1:/tmp/_container_import.py
    # 备份：把现有应用导出成 YAML 写到容器内 /tmp/backup.yaml
    docker exec -e PYTHONPATH=/app/api docker-api-1 python /tmp/_container_import.py export /tmp/backup.yaml
    # 导入：用 /tmp/dify-ai-news-v1.yml 覆盖 APP_ID 指向的应用
    docker exec -e PYTHONPATH=/app/api docker-api-1 python /tmp/_container_import.py import

可用环境变量覆盖默认值：
    DIFY_APP_ID      要覆盖的应用 id，默认 f9474357-2e80-4dec-8636-9fa6e94be922（AI 资讯聚合日报）
    DIFY_IMPORT_YAML 容器内待导入的 DSL 路径

脚本按 Dify 1.17.1 的经典结构写：controllers/console/app/app_import.py 里就是
Session(db.engine) + AppDslService(session).import_app(account=..., ...) 这一套；
account.current_tenant_id 决定导入到哪个 workspace，所以要先挂上 current_tenant。
"""

import json
import os
import sys
import traceback

sys.path.insert(0, "/app/api")

APP_ID = os.environ.get("DIFY_APP_ID", "f9474357-2e80-4dec-8636-9fa6e94be922")
YAML_PATH = os.environ.get("DIFY_IMPORT_YAML", "/tmp/dify-ai-news-v1.yml")

OK_STATUSES = {"completed", "completed-with-warnings", "pending"}


def pick_owner_account_id(session, tenant_id):
    """取该 workspace 的 owner 账号，用它作为导入操作的执行人。"""
    from models.account import TenantAccountJoin

    joins = (
        session.query(TenantAccountJoin)
        .filter(TenantAccountJoin.tenant_id == tenant_id)
        .order_by(TenantAccountJoin.created_at.asc())
        .all()
    )
    for join in joins:
        if (join.role or "").lower() == "owner":
            return join.account_id
    if joins:
        return joins[0].account_id
    raise SystemExit("!! workspace %s 下没有任何账号" % tenant_id)


def main() -> int:
    mode = sys.argv[1] if len(sys.argv) > 1 else "import"
    target = sys.argv[2] if len(sys.argv) > 2 else "/tmp/backup.yaml"

    from app_factory import create_app

    _socketio_app, flask_app = create_app()
    with flask_app.app_context():
        from extensions.ext_database import db
        from models.account import Account
        from models.model import App, Tenant
        from services.app_dsl_service import AppDslService
        from services.entities.dsl_entities import ImportStatus
        from sqlalchemy.orm import Session

        if mode == "list":
            rows = db.session.query(App).order_by(App.created_at.asc()).all()
            print("共 %d 个应用：" % len(rows))
            for row in rows:
                print("  %s | %-10s | %s" % (row.id, row.mode, row.name))
            return 0

        app_model = db.session.get(App, APP_ID)
        if app_model is None:
            print("!! 应用不存在: %s" % APP_ID)
            return 1

        tenant_id = app_model.tenant_id
        account = db.session.get(Account, pick_owner_account_id(db.session, tenant_id))
        account.current_tenant = db.session.get(Tenant, tenant_id)

        print("应用: %s (%s) 模式=%s" % (app_model.name, APP_ID, app_model.mode))
        print("执行账号: %s workspace=%s" % (account.email, tenant_id))

        if mode == "verify":
            from models.workflow import Workflow

            rows = (
                db.session.query(Workflow)
                .filter(Workflow.app_id == APP_ID)
                .order_by(Workflow.created_at.desc())
                .all()
            )
            print("应用 %s 共有 %d 个 workflow 版本：" % (APP_ID, len(rows)))
            for row in rows:
                nodes = (row.graph_dict or {}).get("nodes") or []
                ids = [node.get("id") for node in nodes]
                llm = next((node for node in nodes if node.get("id") == "llm_sum"), None)
                prompt = ""
                if llm:
                    prompt = "".join(part.get("text") or "" for part in llm["data"].get("prompt_template") or [])
                print(
                    "  version=%-28s 版本号=%-4s 节点=%s code_hn=%s 新提示词=%s"
                    % (
                        row.version,
                        row.version_number,
                        len(ids),
                        "code_hn" in ids,
                        "今日头条" in prompt,
                    )
                )
            return 0

        if mode == "dump-llm":
            from models.workflow import Workflow

            rows = (
                db.session.query(Workflow)
                .filter(Workflow.app_id == APP_ID)
                .order_by(Workflow.created_at.desc())
                .all()
            )
            for row in rows[:2]:
                nodes = (row.graph_dict or {}).get("nodes") or []
                llm = next((node for node in nodes if node.get("id") == "llm_sum"), None)
                print("=== version=%s 版本号=%s ===" % (row.version, row.version_number))
                if llm is None:
                    print("  （这一版没有 llm_sum 节点）")
                    continue
                data = dict(llm["data"])
                data.pop("prompt_template", None)
                print(json.dumps(data, ensure_ascii=False, indent=2, default=str))
            return 0

        if mode == "publish":
            from libs.datetime_utils import naive_utc_now
            from services.workflow_service import WorkflowService
            from sqlalchemy.orm import sessionmaker

            # 和 controllers/console/app/workflow.py 的发布接口一样：发一版，再把 app 指向它
            with sessionmaker(db.engine).begin() as session:
                app_in_session = session.get(App, APP_ID)
                workflow = WorkflowService().publish_workflow(
                    session=session,
                    app_model=app_in_session,
                    account=account,
                    marked_name="Codex 更新 LLM 日报提示词",
                    marked_comment="导入 dify-ai-news-v1.yml（LLM 直接输出分档 Markdown 日报）",
                )
                app_in_session.workflow_id = workflow.id
                app_in_session.updated_by = account.id
                app_in_session.updated_at = naive_utc_now()
                print("已发布 version=%s workflow_id=%s" % (workflow.version, workflow.id))
            return 0

        if mode == "export":
            content = AppDslService.export_dsl(app_model, session=db.session, include_secret=False)
            with open(target, "w", encoding="utf-8") as handle:
                handle.write(content)
            print("已导出当前 DSL -> %s（%d 字符）" % (target, len(content)))
            return 0

        if mode != "import":
            print("!! 未知模式 %s（只支持 export / import）" % mode)
            return 1

        with open(YAML_PATH, encoding="utf-8") as handle:
            yaml_content = handle.read()

        # 和 app_import.py 一样：导入路径内部会自己 commit，所以单独开一个 Session
        with Session(db.engine, expire_on_commit=False) as session:
            result = AppDslService(session).import_app(
                account=account,
                import_mode="yaml-content",
                yaml_content=yaml_content,
                app_id=APP_ID,
            )
            if result.status == ImportStatus.FAILED:
                session.rollback()
            else:
                session.commit()

        data = result.model_dump(mode="json")
        print(json.dumps(data, ensure_ascii=False, indent=2))

        if data.get("status") not in OK_STATUSES:
            print("!! 导入未成功")
            return 1
        print("导入完成，状态=%s 应用=%s" % (data.get("status"), data.get("app_id")))
        return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except SystemExit:
        raise
    except Exception:  # noqa: BLE001 - 要把真实堆栈打出来，方便定位
        traceback.print_exc()
        sys.exit(2)
