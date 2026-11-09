"""一个可选模型的运行期视图：所属连接 + 模型条目。"""

from dataclasses import dataclass

from app.models.model_config_file import ModelConnectionEntry, ModelEntry


@dataclass(frozen=True)
class ModelConfigRecord:
    """一个可独立选择和调用的模型。

    运行期消费方经 ``resolve_model`` 一次取齐地址、Key 与能力。两部分都来自模型配置文件
    （见 ``model_config_file``），本对象不另存字段；``id`` 由连接 id 与模型名派生。
    """

    connection: ModelConnectionEntry
    model: ModelEntry

    @property
    def id(self) -> str:
        return self.connection.model_config_id(self.model.model_name)
