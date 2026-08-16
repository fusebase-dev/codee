"""Which tasks provider implementation backs the provider named in settings.

Kept out of :mod:`codee.executor` so the admin UI can build a provider without
importing the executor, which starts an agent pool and a poll loop at import.
"""
from codee_main_context.context import Settings, TasksProvider
from codee_tasks_abstract.provider import AbstractTasksProvider
from codee_tasks_azure_devops.provider import AzureDevOpsTasksProvider
from codee_tasks_jira.provider import JiraTasksProvider

# Concrete tasks providers, keyed by the provider selected in settings. Each
# provider initializes itself from the settings, so nothing here is
# provider-specific.
TASKS_PROVIDERS: dict[TasksProvider, type[AbstractTasksProvider]] = {
    TasksProvider.JIRA: JiraTasksProvider,
    TasksProvider.AZURE_DEVOPS: AzureDevOpsTasksProvider,
}


def build_tasks_provider(settings: Settings) -> AbstractTasksProvider:
    provider = TASKS_PROVIDERS.get(settings.tasks_provider)
    if provider is None:
        raise ValueError(
            f"unsupported tasks provider: {settings.tasks_provider.value}")
    return provider(settings)
