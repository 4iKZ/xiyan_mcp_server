import logging

from openai import AzureOpenAI, OpenAI


def call_openai_sdk(**args):
    key = args["key"]
    base_url = args["url"]
    model = args.get("model", "gpt-3.5-turbo")

    if "azure" in base_url:
        api_version = args.get("api_version", "2025-01-01-preview")
        client = AzureOpenAI(
            api_version=api_version,
            api_key=key,
            azure_endpoint=base_url,
            azure_deployment=model,
            timeout=120.0,  # 120 seconds timeout
        )
        logging.info(f"Configured AzureOpenAI: model={model}, api_version={api_version}")
    else:
        client = OpenAI(
            api_key=key,
            base_url=base_url,
            timeout=120.0,  # 120 seconds timeout
        )
        logging.info(f"Configured OpenAI: model={model}, base_url={base_url}")

    del args["key"]
    del args["url"]
    args.pop("api_version", None)

    logging.debug(f"Calling LLM chat completions with model: {model}")
    completion = client.chat.completions.create(**args)
    return completion
