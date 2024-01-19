try:
    from openai import AzureOpenAI as AzureOpenAIClient
except Exception:
    raise Exception(
        "Please upgrade your OpenAI package to version 1.0.0 or later using the command: pip install --upgrade openai.")

from promptflow._internal import ToolProvider, tool
from promptflow.connections import AzureOpenAIConnection
from promptflow.contracts.types import PromptTemplate
from typing import List, Dict

from promptflow.tools.common import render_jinja_template, handle_openai_error, parse_chat, \
    preprocess_template_string, find_referenced_image_set, convert_to_chat_list, normalize_connection_config, \
    post_process_chat_api_response


GPT4V_VERSION = "vision-preview"


def _get_credential():
    from azure.identity import DefaultAzureCredential
    from azure.ai.ml._azure_environments import _get_default_cloud_name, EndpointURLS, _get_cloud, AzureEnvironments
    cloud_name = _get_default_cloud_name()
    if cloud_name != AzureEnvironments.ENV_DEFAULT:
        cloud = _get_cloud(cloud=cloud_name)
        authority = cloud.get(EndpointURLS.ACTIVE_DIRECTORY_ENDPOINT)
        credential = DefaultAzureCredential(authority=authority, exclude_shared_token_cache_credential=True)
    else:
        credential = DefaultAzureCredential()

    return credential


def _parse_resource_id(resourceId):
    split_parts = resourceId.split("/")
    sub, rg, account = split_parts[2], split_parts[4], split_parts[-1]

    return sub, rg, account


class Deployment:
    def __init__(
        self,
        name: str,
        model_name: str,
        version: str
    ):
        self.name = name
        self.model_name = model_name
        self.version = version


class ListDeploymentsError(Exception):
    def __init__(self, msg):
        super().__init__(msg)


def _build_deployment_dict(item) -> Deployment:
    model = item.properties.model
    return Deployment(item.name, model.name, model.version)


def list_deployment_names(
    subscription_id,
    resource_group_name,
    workspace_name,
    connection: AzureOpenAIConnection = None
) -> List[Dict[str, str]]:
    try:
        # Local does not support dynamic list.
        from azure.mgmt.cognitiveservices import CognitiveServicesManagementClient
        from promptflow.azure.operations._arm_connection_operations import ArmConnectionOperations
    except ImportError:
        return [{"value": ""}]

    try:
        credential = _get_credential()
        try:
            conn = ArmConnectionOperations._build_connection_dict(
                name=connection,
                subscription_id=subscription_id,
                resource_group_name=resource_group_name,
                workspace_name=workspace_name,
                credential=credential
            )
            resource_id = conn.get("value").get('resource_id', "")
            print(f"Connection {connection} resource id: {resource_id}")
            conn_sub, conn_rg, conn_account = _parse_resource_id(resource_id)
        except Exception:
            raise Exception("Failed to get connection resource id.")

        client = CognitiveServicesManagementClient(
            credential=credential,
            subscription_id=conn_sub,
        )
        deployment_collection = client.deployments.list(
            resource_group_name=conn_rg,
            account_name=conn_account,
        )

        res = []
        for item in deployment_collection:
            deployment = _build_deployment_dict(item)
            print(f"Connection {connection} deployments:")
            print(
                f"deployment_name: {deployment.name}, model_name: "
                f"{deployment.model_name}, version: {deployment.version}."
            )
            if deployment.version == GPT4V_VERSION:
                print(f"{deployment.name}:{deployment.version} selected.")
                cur_item = {
                    "value": deployment.name,
                    "display_value": deployment.name,
                }
                res.append(cur_item)

        if not res:
            message = (
                f"No deployments support gpt4v in current connection, please create "
                f"gpt4v deployments or use other connections."
            )
            raise ListDeploymentsError(message)
    except ListDeploymentsError:
        print("raise ListDeploymentsError error")
        raise
    except Exception as e:
        print("raise Exception error")
        message = f"Failed to list deployments with error: {e}"
        raise ListDeploymentsError(message)

    return res


class AzureOpenAI(ToolProvider):
    def __init__(self, connection: AzureOpenAIConnection):
        super().__init__()
        self.connection = connection
        self._connection_dict = normalize_connection_config(self.connection)

        azure_endpoint = self._connection_dict.get("azure_endpoint")
        api_version = self._connection_dict.get("api_version")
        api_key = self._connection_dict.get("api_key")

        self._client = AzureOpenAIClient(azure_endpoint=azure_endpoint, api_version=api_version, api_key=api_key)

    @tool(streaming_option_parameter="stream")
    @handle_openai_error()
    def chat(
        self,
        prompt: PromptTemplate,
        deployment_name: str,
        temperature: float = 1.0,
        top_p: float = 1.0,
        # stream is a hidden to the end user, it is only supposed to be set by the executor.
        stream: bool = False,
        stop: list = None,
        max_tokens: int = None,
        presence_penalty: float = 0,
        frequency_penalty: float = 0,
        **kwargs,
    ) -> str:
        # keep_trailing_newline=True is to keep the last \n in the prompt to avoid converting "user:\t\n" to "user:".
        prompt = preprocess_template_string(prompt)
        referenced_images = find_referenced_image_set(kwargs)

        # convert list type into ChatInputList type
        converted_kwargs = convert_to_chat_list(kwargs)
        chat_str = render_jinja_template(prompt, trim_blocks=True, keep_trailing_newline=True, **converted_kwargs)
        messages = parse_chat(chat_str, list(referenced_images))

        headers = {
            "Content-Type": "application/json",
            "ms-azure-ai-promptflow-called-from": "aoai-gpt4v-tool"
        }

        params = {
            "messages": messages,
            "temperature": temperature,
            "top_p": top_p,
            "n": 1,
            "stream": stream,
            "presence_penalty": presence_penalty,
            "frequency_penalty": frequency_penalty,
            "extra_headers": headers,
            "model": deployment_name,
        }

        if stop:
            params["stop"] = stop
        if max_tokens is not None:
            params["max_tokens"] = max_tokens

        completion = self._client.chat.completions.create(**params)
        return post_process_chat_api_response(completion, stream, None)
