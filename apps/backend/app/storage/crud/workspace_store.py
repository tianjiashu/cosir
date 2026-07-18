"""WorkspaceStoreMixin implementation for SQLiteTaskStore."""

import logging

from app.storage.crud.task_common import *

_LOGGER = logging.getLogger("coding_agent.backend")


class WorkspaceStoreMixin:
    """SQLiteTaskStore WorkspaceStoreMixin responsibilities."""

    def _ensure_default_workspace(self, now: datetime) -> str:
        """纭繚鍏煎鏃т换鍔″叆鍙ｇ殑榛樿宸ヤ綔鍖哄瓨鍦ㄣ€?

        鍙傛暟:
            now: 鐢ㄤ簬鍐欏叆鍒涘缓涓庢洿鏂版椂闂寸殑 UTC 鏃堕棿銆?

        杩斿洖:
            榛樿宸ヤ綔鍖烘爣璇嗙銆?

        寮傚父:
            sqlalchemy.exc.SQLAlchemyError: 濡傛灉鏁版嵁搴撳啓鍏ュけ璐ャ€?

        鍓綔鐢?
            鍦ㄧ己澶辨椂鍐欏叆涓€鏉￠粯璁?workspace 璁板綍銆?
        """

        workspace_id = "default-workspace"
        with self._session_factory.begin() as session:
            if session.get(WorkspaceModel, workspace_id) is None:
                session.add(
                    WorkspaceModel(
                        workspace_id=workspace_id,
                        name="Default Workspace",
                        root_path=".",
                        created_at=_to_text(now),
                        updated_at=_to_text(now),
                    )
                )
        return workspace_id

    def create_workspace(self, name: str, root_path: str) -> WorkspaceRecord:
        """鍒涘缓涓€涓湰鍦板伐浣滃尯璁板綍銆?

        鍙傛暟:
            name: 鐢ㄦ埛鍙鐨勫伐浣滃尯鍚嶇О銆?
            root_path: 宸ヤ綔鍖虹殑鏈湴鏂囦欢绯荤粺璺緞銆?

        杩斿洖:
            宸插垱寤虹殑宸ヤ綔鍖鸿褰曘€?

        寮傚父:
            ValueError: 濡傛灉鍚嶇О鎴栬矾寰勪负绌虹櫧銆?
            sqlalchemy.exc.SQLAlchemyError: 濡傛灉鏁版嵁搴撳啓鍏ュけ璐ャ€?

        鍓綔鐢?
            鍚戜富搴撳啓鍏ヤ竴琛屽伐浣滃尯璁板綍銆?
        """

        if not name.strip():
            raise ValueError("workspace name must not be blank")
        if not root_path.strip():
            raise ValueError("workspace root_path must not be blank")
        now = _utc_now()
        workspace = WorkspaceRecord(str(uuid4()), name.strip(), root_path.strip(), now, now)
        with self._session_factory.begin() as session:
            session.add(
                WorkspaceModel(
                    workspace_id=workspace.workspace_id,
                    name=workspace.name,
                    root_path=workspace.root_path,
                    created_at=_to_text(workspace.created_at),
                    updated_at=_to_text(workspace.updated_at),
                )
            )
        return workspace

    def list_workspaces(self) -> List[WorkspaceRecord]:
        """鎸夊垱寤烘椂闂村垪鍑烘墍鏈夊伐浣滃尯銆?

        鍙傛暟:
            鏃犮€?

        杩斿洖:
            宸ヤ綔鍖鸿褰曞垪琛ㄣ€?

        寮傚父:
            鏃犮€?

        鍓綔鐢?
            鏃犮€?
        """

        with self._session_factory() as session:
            rows = session.execute(
                select(WorkspaceModel).order_by(asc(WorkspaceModel.created_at), asc(WorkspaceModel.workspace_id))
            ).scalars().all()
        return [_workspace_from_model(row) for row in rows]

    def get_workspace(self, workspace_id: str) -> WorkspaceRecord:
        """鎸夋爣璇嗙杩斿洖涓€涓伐浣滃尯銆?

        鍙傛暟:
            workspace_id: 寰呰幏鍙栫殑宸ヤ綔鍖烘爣璇嗙銆?

        杩斿洖:
            鍖归厤鐨勫伐浣滃尯璁板綍銆?

        寮傚父:
            KeyError: 濡傛灉宸ヤ綔鍖轰笉瀛樺湪銆?

        鍓綔鐢?
            鏃犮€?
        """

        with self._session_factory() as session:
            row = session.get(WorkspaceModel, workspace_id)
        if row is None:
            raise KeyError(workspace_id)
        return _workspace_from_model(row)

    def delete_workspace(self, workspace_id: str) -> None:
        """鍒犻櫎宸ヤ綔鍖哄苟绾ц仈鍒犻櫎鍏朵笅浠诲姟杩愯璁板綍銆?

        鍙傛暟:
            workspace_id: 寰呭垹闄ょ殑宸ヤ綔鍖烘爣璇嗙銆?

        杩斿洖:
            鏃犮€?

        寮傚父:
            KeyError: 濡傛灉宸ヤ綔鍖轰笉瀛樺湪銆?

        鍓綔鐢?
            鍒犻櫎 workspace銆乼asks銆乼urns銆乻teps 鍜 events 琛ㄤ腑鐨勫叧鑱旇褰曘€?
        """

        self.get_workspace(workspace_id)
        task_ids: list[str] = []
        turn_ids: list[str] = []
        try:
            with self._session_factory.begin() as session:
                task_ids = [
                    row[0]
                    for row in session.execute(select(TaskModel.task_id).where(TaskModel.workspace_id == workspace_id)).all()
                ]
                if task_ids:
                    turn_ids = [
                        row[0]
                        for row in session.execute(select(TurnModel.turn_id).where(TurnModel.task_id.in_(task_ids))).all()
                    ]
                    # 娉ㄦ剰锛歟vents / steps 澶栭敭鎸囧悜 turns锛屽繀椤诲厛鍒犻櫎锛泃urns 澶栭敭鎸囧悜 tasks锛屾墍浠?
                    # events / steps 閮借鍦?turns 涔嬪墠鍒犻櫎锛屽惁鍒?foreign_keys=ON 浼氳Е鍙戝啿绐併€?
                    session.execute(delete(EventModel).where(EventModel.task_id.in_(task_ids)))
                    if turn_ids:
                        session.execute(delete(StepModel).where(StepModel.turn_id.in_(turn_ids)))
                        session.execute(delete(TurnModel).where(TurnModel.turn_id.in_(turn_ids)))
                    session.execute(delete(TaskModel).where(TaskModel.task_id.in_(task_ids)))
                session.execute(delete(WorkspaceModel).where(WorkspaceModel.workspace_id == workspace_id))
        except Exception:
            _LOGGER.exception(
                "workspace_delete_failed",
                extra={
                    "msg": f"鍒犻櫎宸ヤ綔鍖哄強涓嬫父璁板綍鍐欏叆鏁版嵁搴撳け璐ワ紝workspace_id={workspace_id}",
                    "data": {"workspace_id": workspace_id, "operation": "delete_workspace"},
                },
            )
            raise
        _LOGGER.info(
            "workspace_cascade_deleted",
            extra={
                "msg": f"宸ヤ綔鍖哄強涓嬫父浠诲姟杩愯璁板綍宸插垹闄わ紝workspace_id={workspace_id}",
                "data": {"workspace_id": workspace_id, "task_count": len(task_ids), "turn_count": len(turn_ids)},
            },
        )
