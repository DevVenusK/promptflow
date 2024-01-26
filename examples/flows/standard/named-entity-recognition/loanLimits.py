import os
import openai
from azure.core.credentials import AzureKeyCredential
from azure.search.documents import SearchClient
from promptflow import tool
import json

serviceEndpoint = "https://findasearchhyosung.search.windows.net"
indexName = "moneymaker-loanresult"
key = "viz8ENe7NTt3y5GLVdjHvdszjX5KkQpAygiNDH9fQeAzSeA3OVGY"
credential = AzureKeyCredential(key)

client = SearchClient(
             endpoint=serviceEndpoint,
             index_name=indexName,
             credential=credential
            )
@tool
def loans(users: [json]):
    resultDictionaries = []
    print("wpokerpqowkeproqkwperokqwoerqweopkr", users)
    # applicationIDs = []

    loanLimits = []
    loanRates = []

    for user in users:
        print("User ========******", user)
        jsonUser = json.loads(user)
        applicationID = jsonUser["application_id"]
        # applicationIDs.append(applicationID)
        results = client.search(search_text=applicationID)
        for result in results:
            loanLimits.append(result['loan_limit'])
            loanRates.append(result['loan_rate'])
        
        
    maxLoanLimit = max(loanLimits)
    minLoanRate = min(loanRates)

    return maxLoanLimit, minLoanRate