
import os
import openai
from azure.core.credentials import AzureKeyCredential
from azure.search.documents import SearchClient
from azure.search.documents.indexes import SearchIndexClient
from typing import Dict
from promptflow import tool
import json
from azure.search.documents.indexes.models import (
    ComplexField,
    SearchIndex,
    CorsOptions,
    ScoringProfile,
    DistanceScoringFunction,
    MagnitudeScoringFunction,
    TagScoringFunction,
    FreshnessScoringFunction,
    SearchIndexerDataSourceType
)

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
def specs(user: json):
    dictionaryUser = json.loads(user)
    userCresitScore = dictionaryUser['credit_score']
    userYearlyIncome = dictionaryUser['yearly_income']

    upperCreditScore = float(userCresitScore) + 50
    lowerCreditScore = float(userCresitScore) - 50

    upperYearlyIncome = float(userYearlyIncome) + 10000000
    lowerYearlyIncome = float(userYearlyIncome) - 10000000

    newDictionary: [str, 'Edm.Double'] = {
        'upperCreditScore': upperCreditScore, 
        'lowerCreditScore': lowerCreditScore,
        'upperYearlyImcome': upperYearlyIncome,
        'lowerYearlyImcome': lowerYearlyIncome
        }    
    
    newUpperCreditScore = newDictionary['upperCreditScore']
    newLowerCreditScore = newDictionary['lowerCreditScore']
    newUpperYearlyIncome = newDictionary['upperYearlyImcome']
    newLowerYearlyImcome = newDictionary['lowerYearlyImcome']


    results = client.search(filter=f"credit_score ge {newLowerCreditScore} and credit_score le {newUpperCreditScore} and yearly_income ge {newLowerYearlyImcome} and yearly_income le {newUpperYearlyIncome}")
    resultDictionary = []
    for result in results:
        resultDictionary.append(json.dumps(result))

    return resultDictionary[:10]