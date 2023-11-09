import logging
from typing import List

from localstack.aws.api import CommonServiceException, RequestContext, handler
from localstack.aws.api.cloudwatch import (
    AccountId,
    ActionPrefix,
    AlarmName,
    AlarmNamePrefix,
    AlarmNames,
    AlarmTypes,
    AmazonResourceName,
    CloudwatchApi,
    DescribeAlarmsOutput,
    DimensionFilters,
    GetMetricDataMaxDatapoints,
    GetMetricDataOutput,
    IncludeLinkedAccounts,
    InvalidParameterCombinationException,
    InvalidParameterValueException,
    LabelOptions,
    ListMetricsOutput,
    ListTagsForResourceOutput,
    MaxRecords,
    MetricData,
    MetricDataQueries,
    MetricDataResultMessages,
    MetricDataResults,
    MetricName,
    Namespace,
    NextToken,
    PutMetricAlarmInput,
    RecentlyActive,
    ScanBy,
    StateValue,
    TagKeyList,
    TagList,
    TagResourceOutput,
    Timestamp,
    UntagResourceOutput,
)
from localstack.http import Request
from localstack.services.cloudwatch.alarm_scheduler import AlarmScheduler
from localstack.services.cloudwatch.cloudwatch_database_helper import CloudwatchDatabase
from localstack.services.cloudwatch.models import (
    CloudWatchStore,
    LocalStackMetricAlarm,
    cloudwatch_stores,
)
from localstack.services.edge import ROUTER
from localstack.services.plugins import SERVICE_PLUGINS, ServiceLifecycleHook
from localstack.utils.aws import arns
from localstack.utils.sync import poll_condition
from localstack.utils.tagging import TaggingService
from localstack.utils.threads import start_worker_thread

PATH_GET_RAW_METRICS = "/_aws/cloudwatch/metrics/raw"
MOTO_INITIAL_UNCHECKED_REASON = "Unchecked: Initial alarm creation"

LOG = logging.getLogger(__name__)


class ValidationError(CommonServiceException):
    # TODO: check this error against AWS (doesn't exist in the API)
    def __init__(self, message: str):
        super().__init__("ValidationError", message, 400, True)


def _validate_parameters_for_put_metric_data(metric_data: MetricData) -> None:
    for index, metric_item in enumerate(metric_data):
        indexplusone = index + 1
        if metric_item.get("Value") and metric_item.get("Values"):
            raise InvalidParameterCombinationException(
                f"The parameters MetricData.member.{indexplusone}.Value and MetricData.member.{indexplusone}.Values are mutually exclusive and you have specified both."
            )

        if (values := metric_item.get("Values")) and (counts := metric_item.get("Counts")):
            if len(values) != len(counts):
                raise InvalidParameterValueException(
                    f"The parameters MetricData.member.{indexplusone}.Values and MetricData.member.{indexplusone}.Counts must be of the same size."
                )

        # TODO: check for other validations


class CloudwatchProvider(CloudwatchApi, ServiceLifecycleHook):
    """
    Cloudwatch provider.

    LIMITATIONS:
        - no alarm rule evaluation
    """

    def __init__(self):
        self.tags = TaggingService()
        self.alarm_scheduler: AlarmScheduler = None
        self.store = None
        self.cloudwatch_database = CloudwatchDatabase()

    @staticmethod
    def get_store(account_id: str, region: str) -> CloudWatchStore:
        return cloudwatch_stores[account_id][region]

    def on_after_init(self):
        ROUTER.add(PATH_GET_RAW_METRICS, self.get_raw_metrics)
        self.start_alarm_scheduler()

    def on_before_state_reset(self):
        self.shutdown_alarm_scheduler()
        self.cloudwatch_database.clear_tables()

    def on_after_state_reset(self):
        self.start_alarm_scheduler()

    def on_before_state_load(self):
        self.shutdown_alarm_scheduler()

    def on_after_state_load(self):
        self.start_alarm_scheduler()

        def restart_alarms(*args):
            poll_condition(lambda: SERVICE_PLUGINS.is_running("cloudwatch"))
            self.alarm_scheduler.restart_existing_alarms()

        start_worker_thread(restart_alarms)

    def on_before_stop(self):
        self.shutdown_alarm_scheduler()
        self.cloudwatch_database.shutdown()

    def start_alarm_scheduler(self):
        if not self.alarm_scheduler:
            LOG.debug("starting cloudwatch scheduler")
            self.alarm_scheduler = AlarmScheduler()

    def shutdown_alarm_scheduler(self):
        LOG.debug("stopping cloudwatch scheduler")
        self.alarm_scheduler.shutdown_scheduler()
        self.alarm_scheduler = None

    def delete_alarms(self, context: RequestContext, alarm_names: AlarmNames) -> None:
        """
        Delete alarms.
        """

        for alarm_name in alarm_names:
            alarm_arn = arns.cloudwatch_alarm_arn(alarm_name)  # obtain alarm ARN from alarm name
            self.alarm_scheduler.delete_scheduler_for_alarm(alarm_arn)

    def put_metric_data(
        self, context: RequestContext, namespace: Namespace, metric_data: MetricData
    ) -> None:
        _validate_parameters_for_put_metric_data(metric_data)

        self.cloudwatch_database.add_metric_data(
            context.account_id, context.region, namespace, metric_data
        )

    def get_metric_data(
        self,
        context: RequestContext,
        metric_data_queries: MetricDataQueries,
        start_time: Timestamp,
        end_time: Timestamp,
        next_token: NextToken = None,
        scan_by: ScanBy = None,
        max_datapoints: GetMetricDataMaxDatapoints = None,
        label_options: LabelOptions = None,
    ) -> GetMetricDataOutput:
        results: List[MetricDataResults] = []
        for query in metric_data_queries:
            query_result = self.cloudwatch_database.get_metric_data_stat(
                account_id=context.account_id,
                region=context.region,
                query=query,
                start_time=start_time,
                end_time=end_time,
                scan_by=scan_by,
            )
            results.append(query_result)

        # TODO pagination
        # from localstack.utils.collections import PaginatedList
        #
        # aliases_list = PaginatedList(results)
        # limit = max_datapoints or 100_800
        # page, nxt = aliases_list.get_page(
        #     lambda metric_result: metric_result.get("Id"),
        #     next_token=next_token,
        #     page_size=limit,
        # )
        #
        nxt: NextToken = None
        # TODO might contain error messages if data could not be retrieved, needs testing
        messages: MetricDataResultMessages = None  # TODO

        formatted_results = []
        for result in results:
            formatted_result = {
                "Id": result.get("id"),
                "Label": "TODO",
                "StatusCode": "Complete",
                "Timestamps": [],
                "Values": [],
            }
            datapoints = result.get("datapoints", {})
            for timestamp, datapoint_result in datapoints.items():
                formatted_result["Timestamps"].append(int(timestamp))
                formatted_result["Values"].append(datapoint_result)

            formatted_results.append(formatted_result)

        return GetMetricDataOutput(
            MetricDataResults=formatted_results, NextToken=nxt, Messages=messages
        )

    def get_raw_metrics(self, request: Request):
        # TODO this needs to be read from the database
        # FIXME this is just a placeholder for now
        return {"metrics": []}

    @handler("PutMetricAlarm", expand=False)
    def put_metric_alarm(self, context: RequestContext, request: PutMetricAlarmInput) -> None:
        # missing will be the default, when not set (but it will not explicitly be set)
        if request.get("TreatMissingData", "missing") not in [
            "breaching",
            "notBreaching",
            "ignore",
            "missing",
        ]:
            raise ValidationError(
                f"The value {request['TreatMissingData']} is not supported for TreatMissingData parameter. Supported values are [breaching, notBreaching, ignore, missing]."
            )
            # do some sanity checks:
        if request.get("Period"):
            # Valid values are 10, 30, and any multiple of 60.
            value = request.get("Period")
            if value not in (10, 30):
                if value % 60 != 0:
                    raise ValidationError("Period must be 10, 30 or a multiple of 60")
        if request.get("Statistic"):
            if request.get("Statistic") not in [
                "SampleCount",
                "Average",
                "Sum",
                "Minimum",
                "Maximum",
            ]:
                raise ValidationError(
                    f"Value '{request.get('Statistic')}' at 'statistic' failed to satisfy constraint: Member must satisfy enum value set: [Maximum, SampleCount, Sum, Minimum, Average]"
                )

        extended_statistic = request.get("ExtendedStatistic")
        if extended_statistic and not extended_statistic.startswith("p"):
            raise InvalidParameterValueException(
                f"The value {extended_statistic} for parameter ExtendedStatistic is not supported."
            )
        evaluate_low_sample_count_percentile = request.get("EvaluateLowSampleCountPercentile")
        if evaluate_low_sample_count_percentile and evaluate_low_sample_count_percentile not in (
            "evaluate",
            "ignore",
        ):
            raise ValidationError(
                f"Option {evaluate_low_sample_count_percentile} is not supported. "
                "Supported options for parameter EvaluateLowSampleCountPercentile are evaluate and ignore."
            )

        store = self.get_store(context.account_id, context.region)
        metric_alarm = LocalStackMetricAlarm(context.account_id, context.region, {**request})
        alarm_arn = metric_alarm.alarm["AlarmArn"]
        store.Alarms[alarm_arn] = metric_alarm
        self.alarm_scheduler.schedule_metric_alarm(alarm_arn)

    def describe_alarms(
        self,
        context: RequestContext,
        alarm_names: AlarmNames = None,
        alarm_name_prefix: AlarmNamePrefix = None,
        alarm_types: AlarmTypes = None,
        children_of_alarm_name: AlarmName = None,
        parents_of_alarm_name: AlarmName = None,
        state_value: StateValue = None,
        action_prefix: ActionPrefix = None,
        max_records: MaxRecords = None,
        next_token: NextToken = None,
    ) -> DescribeAlarmsOutput:
        store = self.get_store(context.account_id, context.region)
        alarms = list(store.Alarms.values())
        if action_prefix:
            alarms = [a.alarm for a in alarms if a.alarm["AlarmAction"].startswith(action_prefix)]
        elif alarm_name_prefix:
            alarms = [a.alarm for a in alarms if a.alarm["AlarmName"].startswith(alarm_name_prefix)]
        elif alarm_names:
            alarms = [a.alarm for a in alarms if a.alarm["AlarmName"] in alarm_names]
        elif state_value:
            alarms = [a.alarm for a in alarms if a.alarm["StateValue"] == state_value]
        else:
            alarms = [a.alarm for a in list(store.Alarms.values())]

        # TODO: Pagination
        metric_alarms = [a for a in alarms if a.get("AlarmRule") is None]
        composite_alarms = [a for a in alarms if a.get("AlarmRule") is not None]
        return DescribeAlarmsOutput(CompositeAlarms=composite_alarms, MetricAlarms=metric_alarms)

    def list_tags_for_resource(
        self, context: RequestContext, resource_arn: AmazonResourceName
    ) -> ListTagsForResourceOutput:
        tags = self.tags.list_tags_for_resource(resource_arn)
        return ListTagsForResourceOutput(Tags=tags.get("Tags", []))

    def untag_resource(
        self, context: RequestContext, resource_arn: AmazonResourceName, tag_keys: TagKeyList
    ) -> UntagResourceOutput:
        self.tags.untag_resource(resource_arn, tag_keys)
        return UntagResourceOutput()

    def tag_resource(
        self, context: RequestContext, resource_arn: AmazonResourceName, tags: TagList
    ) -> TagResourceOutput:
        self.tags.tag_resource(resource_arn, tags)
        return TagResourceOutput()

    def list_metrics(
        self,
        context: RequestContext,
        namespace: Namespace = None,
        metric_name: MetricName = None,
        dimensions: DimensionFilters = None,
        next_token: NextToken = None,
        recently_active: RecentlyActive = None,
        include_linked_accounts: IncludeLinkedAccounts = None,
        owning_account: AccountId = None,
    ) -> ListMetricsOutput:
        result = self.cloudwatch_database.list_metrics(
            context.account_id,
            context.region,
            namespace,
            metric_name,
            dimensions or [],
        )

        metrics = [
            {
                "Namespace": metric.get("namespace"),
                "MetricName": metric.get("metric_name"),
                "Dimensions": metric.get("dimensions"),
            }
            for metric in result.get("metrics", [])
        ]
        return ListMetricsOutput(Metrics=metrics, NextToken=None)
