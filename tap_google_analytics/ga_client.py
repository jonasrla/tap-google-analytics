from datetime import timedelta, datetime
import sys
from os import getenv
import backoff
import logging
import json
import singer
import socket

from apiclient.discovery import build
from apiclient.errors import HttpError

from oauth2client.service_account import ServiceAccountCredentials
from oauth2client.client import GoogleCredentials

from tap_google_analytics import error

SCOPES = ['https://www.googleapis.com/auth/analytics.readonly']

NON_FATAL_ERRORS = [
  'userRateLimitExceeded',
  'rateLimitExceeded',
  'quotaExceeded',
  'internalServerError',
  'backendError'
]

DATE_PATTERN = "%Y-%m-%d"

# Silence the discovery_cache errors
logging.getLogger('googleapiclient.discovery_cache').setLevel(logging.ERROR)
LOGGER = singer.get_logger()
LOGGER.setLevel(getenv("LOGGER_LEVEL", "INFO"))

def error_reason(e):
    # For a given HttpError object from the googleapiclient package, this
    # returns the first reason code from
    # https://developers.google.com/analytics/devguides/reporting/core/v4/errors
    # if the error's HTTP response body is valid json. Note that the code
    # samples for Python on that page are actually incorrect, and that
    # e.resp.reason is the HTTP transport level reason associated with the
    # status code, like "Too Many Requests" for a 429 response code, whereas we
    # want the reason field of the first error in the JSON response body.

    reason = ''
    try:
        data = json.loads(e.content.decode('utf-8'))
        reason = data['error']['errors'][0]['reason']
    except Exception:
        pass

    return reason


def is_fatal_error(error):
    if isinstance(error, socket.timeout):
        return False

    status = error.resp.status if getattr(error, 'resp') is not None else None
    if status in [500, 503]:
        return False

    # Use list of errors defined in:
    # https://developers.google.com/analytics/devguides/reporting/core/v4/errors
    reason = error_reason(error)
    if reason in NON_FATAL_ERRORS:
        return False

    LOGGER.critical(
        "Received fatal error %s, reason=%s, status=%s", error, reason, status)
    return True


class GAClient:
    def __init__(self, view_id, config, state):
        yesterday = (datetime.today() - timedelta(days=1)
                     ).strftime(DATE_PATTERN)

        state_date = state.get('bookmarks',
                               {}).get(view_id, {}).get('end_date')

        if state_date is not None:
            state_date = (datetime.strptime(state_date, DATE_PATTERN)
                          + timedelta(days=1)).strftime(DATE_PATTERN)

        self.view_id = view_id
        self.start_date = state_date or config['start_date']
        self.end_date = config['end_date'] or yesterday
        self.quota_user = config.get('quota_user', None)
        self.is_sliced = config.get('sliced', False)

        self.credentials = self.initialize_credentials(config)
        self.analytics = self.initialize_analyticsreporting()

        (self.dimensions_ref, self.metrics_ref) = self.fetch_metadata()

        LOGGER.debug(
            f"GAClient view_id: {view_id}, time interval from {self.start_date} to \
{self.end_date}")


    def initialize_credentials(self, config):
        if config.get('oauth_credentials', {}).get('access_token', None):
            return GoogleCredentials(
                access_token=config['oauth_credentials']['access_token'],
                refresh_token=config['oauth_credentials']['refresh_token'],
                client_id=config['oauth_credentials']['client_id'],
                client_secret=config['oauth_credentials']['client_secret'],
                # let the library refresh the token if it is expired
                token_expiry=None,
                token_uri="https://accounts.google.com/o/oauth2/token",
                user_agent="tap-google-analytics (via singer.io)"
            )
        else:
            return ServiceAccountCredentials \
                .from_json_keyfile_dict(config['client_secrets'], SCOPES)

    def initialize_analyticsreporting(self):
        """Initializes an Analytics Reporting API V4 service object.

        Returns:
            An authorized Analytics Reporting API V4 service object.
        """
        return build('analyticsreporting', 'v4', credentials=self.credentials)

    def fetch_metadata(self):
        """
        Fetch the valid (dimensions, metrics) for the Analytics Reporting API
         and their data types.

        Returns:
          A map of (dimensions, metrics) hashes

          Each available dimension can be found in dimensions with its data
            type as the value. e.g. dimensions['ga:userType'] == STRING

          Each available metric can be found in metrics with its data type
            as the value. e.g. metrics['ga:sessions'] == INTEGER
        """
        metrics = {}
        dimensions = {}

        # Initialize a Google Analytics API V3 service object and build the
        # service object. This is needed in order to dynamically fetch the
        # metadata for available metrics and dimensions.
        # (those are not provided in the Analytics Reporting API V4)
        service = build('analytics', 'v3', credentials=self.credentials)

        results = service.metadata().columns().list(
            reportType='ga', quotaUser=self.quota_user).execute()

        columns = results.get('items', [])

        for column in columns:
            column_attributes = column.get('attributes', [])

            column_name = column.get('id')
            column_type = column_attributes.get('type')
            column_data_type = column_attributes.get('dataType')

            if column_type == 'METRIC':
                metrics[column_name] = column_data_type
            elif column_type == 'DIMENSION':
                dimensions[column_name] = column_data_type

        return (dimensions, metrics)

    def lookup_data_type(self, type, attribute):
        """
        Get the data type of a metric or a dimension
        """
        try:
            if type == 'dimension':
                if attribute.startswith(('ga:dimension',
                                         'ga:customVarName',
                                         'ga:customVarValue')):
                    # Custom Google Analytics Dimensions that are not part of
                    #  self.dimensions_ref. They are always strings
                    return 'string'

                attr_type = self.dimensions_ref[attribute]
            elif type == 'metric':
                # Custom Google Analytics Metrics
                # {ga:goalXXStarts, ga:metricXX, ... }
                # We always treat them as as strings as we can not be sure of
                # their data type
                if attribute.startswith('ga:goal') and \
                        attribute.endswith(('Starts',
                                            'Completions',
                                            'Value',
                                            'ConversionRate',
                                            'Abandons',
                                            'AbandonRate')):
                    return 'string'
                elif attribute.startswith('ga:searchGoal') and \
                        attribute.endswith('ConversionRate'):
                    # Custom Google Analytics Metrics
                    # ga:searchGoalXXConversionRate
                    return 'string'
                elif attribute.startswith(('ga:metric', 'ga:calcMetric')):
                    return 'string'

                attr_type = self.metrics_ref[attribute]
            else:
                LOGGER.critical(f"Unsuported GA type: {type}")
                sys.exit(1)
        except KeyError:
            LOGGER.critical(f"Unsuported GA {type}: {attribute}")
            sys.exit(1)

        data_type = 'string'

        if attr_type == 'INTEGER':
            data_type = 'integer'
        elif attr_type == 'FLOAT' or \
                attr_type == 'PERCENT' or \
                attr_type == 'TIME':
            data_type = 'number'

        return data_type

    def get_dates(self):
        date = datetime.strptime(self.start_date, "%Y-%m-%d")
        while date < datetime.strptime(self.end_date, "%Y-%m-%d"):
            yield date.strftime("%Y-%m-%d")
            date += timedelta(days=1)


    def process_stream(self, stream):
        """
        Retrives data from Google Analytics
        Inputs:
            stream:
                {"metrics": ["ga_sessions", ...],
                 "dimensions": ["ga_sourceMedium", ...]
                }

        Returns:
            records:
                [
                 {'ga_date': '20190501',
                  'ga_30dayUsers': '134420',
                  'report_start_date': '2019-05-01',
                  'report_end_date': '2019-05-28'},
                  ... ... ...
                ]
        """
        try:
            records = []
            report_definition = self.generate_report_definition(stream)
            nextPageToken = None

            while True:
                if self.is_sliced:
                    for d in self.get_dates():
                        response = self.query_api(report_definition, nextPageToken, d)
                        (nextPageToken, results) = self.process_response(response)
                        records.extend(results)
                else:
                    response = self.query_api(report_definition, nextPageToken)
                    (nextPageToken, results) = self.process_response(response)
                    records.extend(results)

                # Keep on looping as long as we have a nextPageToken
                if nextPageToken is None:
                    break

            return records
        except HttpError as e:
            # Process API errors
            # Use list of errors defined in:
            # https://developers.google.com/analytics/devguides/reporting/core/v4/errors

            reason = error_reason(e)
            if reason == 'userRateLimitExceeded' or \
                    reason == 'rateLimitExceeded':
                raise error.TapGaRateLimitError(e._get_reason())
            elif reason == 'quotaExceeded':
                raise error.TapGaQuotaExceededError(e._get_reason())
            elif e.resp.status == 400:
                raise error.TapGaInvalidArgumentError(e._get_reason())
            elif e.resp.status in [401, 402]:
                raise error.TapGaAuthenticationError(e._get_reason())
            elif e.resp.status in [500, 503]:
                raise error.TapGaBackendServerError(e._get_reason())
            else:
                raise error.TapGaUnknownError(e._get_reason())

    def generate_report_definition(self, stream):
        return {
            'metrics': [{'expression': metric.replace("ga_", "ga:")}
                        for metric in stream['metrics']],
            'dimensions': [{'name': dimension.replace("ga_", "ga:")}
                           for dimension in stream['dimensions']]
        }

    @backoff.on_exception(backoff.expo,
                          (HttpError, socket.timeout),
                          max_tries=9,
                          giveup=is_fatal_error)
    def query_api(self, report_definition, pageToken=None, date=None):
        """Queries the Analytics Reporting API V4.

        Returns:
            The Analytics Reporting API V4 response.
        """
        start_date = date or self.start_date
        end_date = date or self.end_date
        return self.analytics.reports().batchGet(
            body={
                'reportRequests': [
                    {
                        'viewId': self.view_id,
                        'dateRanges': [{'startDate': start_date,
                                        'endDate': end_date}],
                        'pageSize': '1000',
                        'pageToken': pageToken,
                        'metrics': report_definition['metrics'],
                        'dimensions': report_definition['dimensions'],
                        }]
            },
            quotaUser=self.quota_user
        ).execute()

    def process_response(self, response):
        """Processes the Analytics Reporting API V4 response.

        Args:
            response: An Analytics Reporting API V4 response.

        Returns: (nextPageToken, results)
            nextPageToken: The next Page Token
             If it is not None then the maximum pageSize has been reached
             and a followup call must be made using self.query_api().
            results: the Analytics Reporting API V4 response as a list of
             dictionaries, e.g.
             [
              {'ga_date': '20190501',
               'ga_30dayUsers': '134420',
               'report_start_date': '2019-05-01',
               'report_end_date': '2019-05-28'},
               ... ... ...
             ]
        """
        results = []

        try:
            # We always request one report at a time
            report = next(iter(response.get('reports', [])), None)

            columnHeader = report.get('columnHeader', {})
            dimensionHeaders = columnHeader.get('dimensions', [])
            metricHeaders = columnHeader.get(
                'metricHeader', {}).get('metricHeaderEntries', [])

            for row in report.get('data', {}).get('rows', []):
                record = {}
                dimensions = row.get('dimensions', [])
                dateRangeValues = row.get('metrics', [])

                for header, dimension in zip(dimensionHeaders, dimensions):
                    data_type = self.lookup_data_type('dimension', header)

                    if data_type == 'integer':
                        value = int(dimension)
                    elif data_type == 'number':
                        value = float(dimension)
                    else:
                        value = dimension

                    record[header.replace("ga:", "ga_")] = value

                for i, values in enumerate(dateRangeValues):
                    for metricHeader, value in zip(metricHeaders,
                                                   values.get('values')):
                        metric_name = metricHeader.get('name')
                        metric_type = self.lookup_data_type(
                            'metric', metric_name)

                        if metric_type == 'integer':
                            value = int(value)
                        elif metric_type == 'number':
                            value = float(value)

                        record[metric_name.replace("ga:", "ga_")] = value

                # Also add the [start_date,end_date) used for the report
                record['report_start_date'] = self.start_date
                record['report_end_date'] = self.end_date

                results.append(record)

            return (report.get('nextPageToken'), results)
        except StopIteration:
            return (None, [])
