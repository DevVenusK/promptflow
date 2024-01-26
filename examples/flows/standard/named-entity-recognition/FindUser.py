import os
import openai
from azure.core.credentials import AzureKeyCredential
from azure.search.documents import SearchClient
from promptflow import tool
import json

serviceEndpoint = "https://findasearchhyosung.search.windows.net"
indexName = "azureblob-index"
key = "viz8ENe7NTt3y5GLVdjHvdszjX5KkQpAygiNDH9fQeAzSeA3OVGY"
credential = AzureKeyCredential(key)

client = SearchClient(
             endpoint=serviceEndpoint,
             index_name=indexName,
             credential=credential
            )
@tool
def user(userID: str):
    results = client.search(search_text=userID)
    resultDictionary = []
    for result in results:
        resultDictionary.append(json.dumps(result))
    return resultDictionary[0]