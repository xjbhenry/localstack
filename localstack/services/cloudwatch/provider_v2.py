import datetime
import json
import logging

from localstack.aws.api import CommonServiceException, RequestContext, handler
from localstack.aws.api.cloudwatch import (
    ActionPrefix,
    AlarmName,
    AlarmNamePrefix,
    AlarmNames,
    AlarmTypes,
    AmazonResourceName,
    CloudwatchApi,
    DescribeAlarmsOutput,
    HistoryItemType,
    InvalidParameterValueException,
    ListTagsForResourceOutput,
    MaxRecords,
    NextToken,
    PutMetricAlarmInput,
    StateReason,
    StateReasonData,
    StateValue,
    TagKeyList,
    TagList,
    TagResourceOutput,
    UntagResourceOutput,
)
from localstack.aws.connect import connect_to
from localstack.http import Request
from localstack.services.cloudwatch.alarm_scheduler import AlarmScheduler
from localstack.services.cloudwatch.models import (
    CloudWatchStore,
    LocalStackAlarm,
    LocalStackMetricAlarm,
    cloudwatch_stores,
)
from localstack.services.edge import ROUTER
from localstack.services.plugins import SERVICE_PLUGINS, ServiceLifecycleHook
from localstack.utils.aws import arns
from localstack.utils.json import CustomEncoder as JSONEncoder
from localstack.utils.sync import poll_condition
from localstack.utils.tagging import TaggingService
from localstack.utils.threads import start_worker_thread
from localstack.utils.time import timestamp_millis

PATH_GET_RAW_METRICS = "/_aws/cloudwatch/metrics/raw"
DEPRECATED_PATH_GET_RAW_METRICS = "/cloudwatch/metrics/raw"
MOTO_INITIAL_UNCHECKED_REASON = "Unchecked: Initial alarm creation"


LOG = logging.getLogger(__name__)


class ValidationError(CommonServiceException):
    # TODO: check this error against AWS (doesn't exist in the API)
    def __init__(self, message: str):
        super().__init__("ValidationError", message, 400, True)


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

    @staticmethod
    def get_store(account_id: str, region: str) -> CloudWatchStore:
        return cloudwatch_stores[account_id][region]

    def on_after_init(self):
        ROUTER.add(PATH_GET_RAW_METRICS, self.get_raw_metrics)
        self.start_alarm_scheduler()

    def on_before_state_reset(self):
        self.shutdown_alarm_scheduler()

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
            alarm_arn = arns.cloudwatch_alarm_arn(
                alarm_name, account_id=context.account_id, region_name=context.region
            )  # obtain alarm ARN from alarm name
            self.alarm_scheduler.delete_scheduler_for_alarm(alarm_arn)

    def set_alarm_state(
        self,
        context: RequestContext,
        alarm_name: AlarmName,
        state_value: StateValue,
        state_reason: StateReason,
        state_reason_data: StateReasonData = None,
    ) -> None:
        try:
            if state_reason_data:
                state_reason_data = json.loads(state_reason_data)
        except ValueError:
            raise InvalidParameterValueException(
                "TODO: right error message: Json was not correctly formatted"
            )

        store = self.get_store(context.account_id, context.region)
        alarm = store.Alarms.get(
            arns.cloudwatch_alarm_arn(
                alarm_name, account_id=context.account_id, region_name=context.region
            )
        )
        old_state = alarm.alarm["StateValue"]
        if not alarm:
            raise InvalidParameterValueException(
                f"TODO: proper exception: Alarm with name {alarm_name} could not be found"
            )

        if state_value not in ("OK", "ALARM", "INSUFFICIENT_DATA"):
            raise ValidationError(
                f"TODO: right error message: '{state_value}' must be one of INSUFFICIENT_DATA, ALARM, OK"
            )

        self._update_state(context, alarm, state_value, state_reason, state_reason_data)

        if not alarm.alarm["ActionsEnabled"] or old_state == state_value:
            return
        if state_value == "OK":
            actions = alarm.alarm["OKActions"]
        elif state_value == "ALARM":
            actions = alarm.alarm["AlarmActions"]
        else:
            actions = alarm.alarm["InsufficientDataActions"]
        for action in actions:
            data = arns.parse_arn(action)
            # test for sns - can this be done in a more generic way?
            if data["service"] == "sns":
                service = connect_to.get_client(data["service"])
                subject = f"""{state_value}: "{alarm_name}" in {context.region}"""
                message = self.create_message_response_update_state(context, alarm, old_state)
                service.publish(TopicArn=action, Subject=subject, Message=message)
            else:
                # TODO: support other actions
                LOG.warning(
                    "Action for service %s not implemented, action '%s' will not be triggered.",
                    data["service"],
                    action,
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

    def _update_state(
        self,
        context: RequestContext,
        alarm: LocalStackAlarm,
        state_value: str,
        state_reason: str,
        state_reason_data: dict = None,
    ):
        old_state = alarm.alarm["StateValue"]
        store = self.get_store(context.account_id, context.region)
        current_time = datetime.datetime.now()
        store.Histories.append(
            {
                # TODO: check time format
                "Timestamp": timestamp_millis(alarm.alarm["StateUpdatedTimestamp"]),
                "HistoryItemType": HistoryItemType.StateUpdate,
                "AlarmName": alarm.alarm["AlarmName"],
                "HistoryData": alarm.alarm.get("StateReasonData"),  # FIXME
                "HistorySummary": f"Alarm updated from {old_state} to {state_value}",
            }
        )
        alarm.alarm["StateValue"] = state_value
        alarm.alarm["StateReason"] = state_reason
        alarm.alarm["StateReasonData"] = state_reason_data
        alarm.alarm["StateUpdatedTimestamp"] = current_time

    @staticmethod
    def create_message_response_update_state(
        context: RequestContext, alarm: LocalStackAlarm, old_state
    ):
        alarm = alarm.alarm
        response = {
            "AWSAccountId": context.account_id,
            "OldStateValue": old_state,
            "AlarmName": alarm["AlarmName"],
            "AlarmDescription": alarm.get("AlarmDescription"),
            "AlarmConfigurationUpdatedTimestamp": alarm["AlarmConfigurationUpdatedTimestamp"],
            "NewStateValue": alarm["StateValue"],
            "NewStateReason": alarm["StateReason"],
            "StateChangeTime": alarm["StateUpdatedTimestamp"],
            # the long-name for 'region' should be used - as we don't have it, we use the short name
            # which needs to be slightly changed to make snapshot tests work
            "Region": context.region.replace("-", " ").capitalize(),
            "AlarmArn": alarm["AlarmArn"],
            "OKActions": alarm.get("OKActions", []),
            "AlarmActions": alarm.get("AlarmActions", []),
            "InsufficientDataActions": alarm.get("InsufficientDataActions", []),
        }

        # collect trigger details
        details = {
            "MetricName": alarm.get("MetricName", ""),
            "Namespace": alarm.get("Namespace", ""),
            "Unit": alarm.get("Unit", ""),
            "Period": int(alarm.get("Period", 0)),
            "EvaluationPeriods": int(alarm.get("EvaluationPeriods", 0)),
            "ComparisonOperator": alarm.get("ComparisonOperator", ""),
            "Threshold": float(alarm.get("Threshold", 0.0)),
            "TreatMissingData": alarm.get("TreatMissingData", ""),
            "EvaluateLowSampleCountPercentile": alarm.get("EvaluateLowSampleCountPercentile", ""),
        }

        # Dimensions not serializable # TODO: check
        dimensions = []
        alarm_dimensions = alarm.get("Dimensions", [])
        if alarm_dimensions:
            for d in alarm["Dimensions"]:
                dimensions.append({"value": d["Value"], "name": d["Name"]})
        details["Dimensions"] = dimensions or ""

        alarm_statistic = alarm.get("Statistic")
        alarm_extended_statistic = alarm.get("ExtendedStatistic")

        if alarm_statistic:
            details["StatisticType"] = "Statistic"
            details["Statistic"] = alarm_statistic.upper()  # AWS returns uppercase
        elif alarm_extended_statistic:
            details["StatisticType"] = "ExtendedStatistic"
            details["ExtendedStatistic"] = alarm_extended_statistic

        response["Trigger"] = details

        return json.dumps(response, cls=JSONEncoder)

    def disable_alarm_actions(self, context: RequestContext, alarm_names: AlarmNames) -> None:
        self._set_alarm_actions(context, alarm_names, enabled=False)

    def enable_alarm_actions(self, context: RequestContext, alarm_names: AlarmNames) -> None:
        self._set_alarm_actions(context, alarm_names, enabled=True)

    def _set_alarm_actions(self, context, alarm_names, enabled):
        store = self.get_store(context.account_id, context.region)
        for name in alarm_names:
            alarm_arn = arns.cloudwatch_alarm_arn(
                name, account_id=context.account_id, region_name=context.region
            )
            alarm = store.Alarms.get(alarm_arn)
            if alarm:
                alarm.alarm["ActionsEnabled"] = enabled
