import logging
import uuid
from contextlib import contextmanager

import math
import posixpath
from alembic.script import ScriptDirectory
import sqlalchemy

from mlflow.entities.lifecycle_stage import LifecycleStage
from mlflow.store import SEARCH_MAX_RESULTS_THRESHOLD
from mlflow.store.dbmodels.db_types import MYSQL
from mlflow.store.dbmodels.models import Base, SqlExperiment, SqlRun, SqlMetric, SqlParam, SqlTag, \
    SqlExperimentTag, SqlLatestMetric
from mlflow.entities import RunStatus, SourceType, Experiment
from mlflow.store.abstract_store import AbstractStore
from mlflow.entities import ViewType
from mlflow.exceptions import MlflowException
from mlflow.protos.databricks_pb2 import INVALID_PARAMETER_VALUE, RESOURCE_ALREADY_EXISTS, \
    INVALID_STATE, RESOURCE_DOES_NOT_EXIST, INTERNAL_ERROR
from mlflow.tracking.utils import _is_local_uri
from mlflow.utils import extract_db_type_from_uri
from mlflow.utils.file_utils import mkdir, local_file_uri_to_path
from mlflow.utils.search_utils import SearchUtils
from mlflow.utils.validation import _validate_batch_log_limits, _validate_batch_log_data, \
    _validate_run_id, _validate_metric, _validate_experiment_tag, _validate_tag
from mlflow.store.db.utils import _upgrade_db, _get_alembic_config, _get_schema_version
from mlflow.store.dbmodels.initial_models import Base as InitialBase


_logger = logging.getLogger(__name__)


class SqlAlchemyStore(AbstractStore):
    """
    SQLAlchemy compliant backend store for tracking meta data for MLflow entities. MLflow
    supports the database dialects ``mysql``, ``mssql``, ``sqlite``, and ``postgresql``.
    As specified in the
    `SQLAlchemy docs <https://docs.sqlalchemy.org/en/latest/core/engines.html#database-urls>`_ ,
    the database URI is expected in the format
    ``<dialect>+<driver>://<username>:<password>@<host>:<port>/<database>``. If you do not
    specify a driver, SQLAlchemy uses a dialect's default driver.

    This store interacts with SQL store using SQLAlchemy abstractions defined for MLflow entities.
    :py:class:`mlflow.store.dbmodels.models.SqlExperiment`,
    :py:class:`mlflow.store.dbmodels.models.SqlRun`,
    :py:class:`mlflow.store.dbmodels.models.SqlTag`,
    :py:class:`mlflow.store.dbmodels.models.SqlMetric`, and
    :py:class:`mlflow.store.dbmodels.models.SqlParam`.

    Run artifacts are stored in a separate location using artifact stores conforming to
    :py:class:`mlflow.store.artifact_repo.ArtifactRepository`. Default artifact locations for
    user experiments are stored in the database along with metadata. Each run artifact location
    is recorded in :py:class:`mlflow.store.dbmodels.models.SqlRun` and stored in the backend DB.
    """
    ARTIFACTS_FOLDER_NAME = "artifacts"
    DEFAULT_EXPERIMENT_ID = "0"

    def __init__(self, db_uri, default_artifact_root):
        """
        Create a database backed store.

        :param db_uri: The SQLAlchemy database URI string to connect to the database. See
                       the `SQLAlchemy docs
                       <https://docs.sqlalchemy.org/en/latest/core/engines.html#database-urls>`_
                       for format specifications. Mlflow supports the dialects ``mysql``,
                       ``mssql``, ``sqlite``, and ``postgresql``.
        :param default_artifact_root: Path/URI to location suitable for large data (such as a blob
                                      store object, DBFS path, or shared NFS file system).
        """
        super(SqlAlchemyStore, self).__init__()
        self.db_uri = db_uri
        self.db_type = extract_db_type_from_uri(db_uri)
        self.artifact_root_uri = default_artifact_root
        self.engine = sqlalchemy.create_engine(db_uri, pool_pre_ping=True)
        insp = sqlalchemy.inspect(self.engine)
        # On a completely fresh MLflow installation against an empty database (verify database
        # emptiness by checking that 'experiments' etc aren't in the list of table names), run all
        # DB migrations
        expected_tables = set([
            SqlExperiment.__tablename__,
            SqlRun.__tablename__,
            SqlMetric.__tablename__,
            SqlParam.__tablename__,
            SqlTag.__tablename__,
            SqlExperimentTag.__tablename__,
            SqlLatestMetric.__tablename__,
        ])
        if len(expected_tables & set(insp.get_table_names())) == 0:
            SqlAlchemyStore._initialize_tables(self.engine)
        Base.metadata.bind = self.engine
        SessionMaker = sqlalchemy.orm.sessionmaker(bind=self.engine)
        self.ManagedSessionMaker = self._get_managed_session_maker(SessionMaker)
        SqlAlchemyStore._verify_schema(self.engine)

        if _is_local_uri(default_artifact_root):
            mkdir(local_file_uri_to_path(default_artifact_root))

        if len(self.list_experiments()) == 0:
            with self.ManagedSessionMaker() as session:
                self._create_default_experiment(session)

    @staticmethod
    def _initialize_tables(engine):
        _logger.info("Creating initial MLflow database tables...")
        InitialBase.metadata.create_all(engine)
        engine_url = str(engine.url)
        _upgrade_db(engine_url)

    @staticmethod
    def _get_latest_schema_revision():
        """Get latest schema revision as a string."""
        # We aren't executing any commands against a DB, so we leave the DB URL unspecified
        config = _get_alembic_config(db_url="")
        script = ScriptDirectory.from_config(config)
        heads = script.get_heads()
        if len(heads) != 1:
            raise MlflowException("Migration script directory was in unexpected state. Got %s head "
                                  "database versions but expected only 1. Found versions: %s"
                                  % (len(heads), heads))
        return heads[0]

    @staticmethod
    def _verify_schema(engine):
        head_revision = SqlAlchemyStore._get_latest_schema_revision()
        current_rev = _get_schema_version(engine)
        if current_rev != head_revision:
            raise MlflowException(
                "Detected out-of-date database schema (found version %s, but expected %s). "
                "Take a backup of your database, then run 'mlflow db upgrade <database_uri>' "
                "to migrate your database to the latest schema. NOTE: schema migration may "
                "result in database downtime - please consult your database's documentation for "
                "more detail." % (current_rev, head_revision))

    @staticmethod
    def _get_managed_session_maker(SessionMaker):
        """
        Creates a factory for producing exception-safe SQLAlchemy sessions that are made available
        using a context manager. Any session produced by this factory is automatically committed
        if no exceptions are encountered within its associated context. If an exception is
        encountered, the session is rolled back. Finally, any session produced by this factory is
        automatically closed when the session's associated context is exited.
        """

        @contextmanager
        def make_managed_session():
            """Provide a transactional scope around a series of operations."""
            session = SessionMaker()
            try:
                yield session
                session.commit()
            except MlflowException:
                session.rollback()
                raise
            except Exception as e:
                session.rollback()
                raise MlflowException(message=e, error_code=INTERNAL_ERROR)
            finally:
                session.close()

        return make_managed_session

    def _set_no_auto_for_zero_values(self, session):
        if self.db_type == MYSQL:
            session.execute("SET @@SESSION.sql_mode='NO_AUTO_VALUE_ON_ZERO';")

    # DB helper methods to allow zero values for columns with auto increments
    def _unset_no_auto_for_zero_values(self, session):
        if self.db_type == MYSQL:
            session.execute("SET @@SESSION.sql_mode='';")

    def _create_default_experiment(self, session):
        """
        MLflow UI and client code expects a default experiment with ID 0.
        This method uses SQL insert statement to create the default experiment as a hack, since
        experiment table uses 'experiment_id' column is a PK and is also set to auto increment.
        MySQL and other implementation do not allow value '0' for such cases.

        ToDo: Identify a less hacky mechanism to create default experiment 0
        """
        table = SqlExperiment.__tablename__
        default_experiment = {
            SqlExperiment.experiment_id.name: int(SqlAlchemyStore.DEFAULT_EXPERIMENT_ID),
            SqlExperiment.name.name: Experiment.DEFAULT_EXPERIMENT_NAME,
            SqlExperiment.artifact_location.name: str(self._get_artifact_location(0)),
            SqlExperiment.lifecycle_stage.name: LifecycleStage.ACTIVE
        }

        def decorate(s):
            if isinstance(s, str):
                return "'{}'".format(s)
            else:
                return "{}".format(s)

        # Get a list of keys to ensure we have a deterministic ordering
        columns = list(default_experiment.keys())
        values = ", ".join([decorate(default_experiment.get(c)) for c in columns])

        try:
            self._set_no_auto_for_zero_values(session)
            session.execute("INSERT INTO {} ({}) VALUES ({});".format(
                table, ", ".join(columns), values))
        finally:
            self._unset_no_auto_for_zero_values(session)

    def _save_to_db(self, session, objs):
        """
        Store in db
        """
        if type(objs) is list:
            session.add_all(objs)
        else:
            # single object
            session.add(objs)

    def _get_or_create(self, session, model, **kwargs):
        instance = session.query(model).filter_by(**kwargs).first()
        created = False

        if instance:
            return instance, created
        else:
            instance = model(**kwargs)
            self._save_to_db(objs=instance, session=session)
            created = True

        return instance, created

    def _get_artifact_location(self, experiment_id):
        return posixpath.join(self.artifact_root_uri, str(experiment_id))

    def create_experiment(self, name, artifact_location=None):
        if name is None or name == '':
            raise MlflowException('Invalid experiment name', INVALID_PARAMETER_VALUE)

        with self.ManagedSessionMaker() as session:
            try:
                experiment = SqlExperiment(
                    name=name, lifecycle_stage=LifecycleStage.ACTIVE,
                    artifact_location=artifact_location
                )
                session.add(experiment)
                if not artifact_location:
                    # this requires a double write. The first one to generate an autoincrement-ed ID
                    eid = session.query(SqlExperiment).filter_by(name=name).first().experiment_id
                    experiment.artifact_location = self._get_artifact_location(eid)
            except sqlalchemy.exc.IntegrityError as e:
                raise MlflowException('Experiment(name={}) already exists. '
                                      'Error: {}'.format(name, str(e)), RESOURCE_ALREADY_EXISTS)

            session.flush()
            return str(experiment.experiment_id)

    def _list_experiments(self, session, ids=None, names=None, view_type=ViewType.ACTIVE_ONLY):
        stages = LifecycleStage.view_type_to_stages(view_type)
        conditions = [SqlExperiment.lifecycle_stage.in_(stages)]

        if ids and len(ids) > 0:
            int_ids = [int(eid) for eid in ids]
            conditions.append(SqlExperiment.experiment_id.in_(int_ids))

        if names and len(names) > 0:
            conditions.append(SqlExperiment.name.in_(names))
        return session.query(SqlExperiment).filter(*conditions)

    def list_experiments(self, view_type=ViewType.ACTIVE_ONLY):
        with self.ManagedSessionMaker() as session:
            return [exp.to_mlflow_entity() for exp in
                    self._list_experiments(session=session, view_type=view_type)]

    def _get_experiment(self, session, experiment_id, view_type):
        experiment_id = experiment_id or SqlAlchemyStore.DEFAULT_EXPERIMENT_ID
        experiments = self._list_experiments(
            session=session, ids=[experiment_id], view_type=view_type).all()
        if len(experiments) == 0:
            raise MlflowException('No Experiment with id={} exists'.format(experiment_id),
                                  RESOURCE_DOES_NOT_EXIST)
        if len(experiments) > 1:
            raise MlflowException('Expected only 1 experiment with id={}. Found {}.'.format(
                experiment_id, len(experiments)), INVALID_STATE)

        return experiments[0]

    def get_experiment(self, experiment_id):
        with self.ManagedSessionMaker() as session:
            return self._get_experiment(session, experiment_id, ViewType.ALL).to_mlflow_entity()

    def get_experiment_by_name(self, experiment_name):
        """
        Specialized implementation for SQL backed store.
        """
        with self.ManagedSessionMaker() as session:
            experiments = self._list_experiments(
                names=[experiment_name], view_type=ViewType.ALL, session=session).all()
            if len(experiments) == 0:
                return None

            if len(experiments) > 1:
                raise MlflowException('Expected only 1 experiment with name={}. Found {}.'.format(
                    experiment_name, len(experiments)), INVALID_STATE)

            return experiments[0].to_mlflow_entity()

    def delete_experiment(self, experiment_id):
        with self.ManagedSessionMaker() as session:
            experiment = self._get_experiment(session, experiment_id, ViewType.ACTIVE_ONLY)
            experiment.lifecycle_stage = LifecycleStage.DELETED
            self._save_to_db(objs=experiment, session=session)

    def restore_experiment(self, experiment_id):
        with self.ManagedSessionMaker() as session:
            experiment = self._get_experiment(session, experiment_id, ViewType.DELETED_ONLY)
            experiment.lifecycle_stage = LifecycleStage.ACTIVE
            self._save_to_db(objs=experiment, session=session)

    def rename_experiment(self, experiment_id, new_name):
        with self.ManagedSessionMaker() as session:
            experiment = self._get_experiment(session, experiment_id, ViewType.ALL)
            if experiment.lifecycle_stage != LifecycleStage.ACTIVE:
                raise MlflowException('Cannot rename a non-active experiment.', INVALID_STATE)

            experiment.name = new_name
            self._save_to_db(objs=experiment, session=session)

    def create_run(self, experiment_id, user_id, start_time, tags):
        with self.ManagedSessionMaker() as session:
            experiment = self.get_experiment(experiment_id)
            self._check_experiment_is_active(experiment)

            run_id = uuid.uuid4().hex
            artifact_location = posixpath.join(experiment.artifact_location, run_id,
                                               SqlAlchemyStore.ARTIFACTS_FOLDER_NAME)
            run = SqlRun(name="", artifact_uri=artifact_location, run_uuid=run_id,
                         experiment_id=experiment_id,
                         source_type=SourceType.to_string(SourceType.UNKNOWN),
                         source_name="", entry_point_name="",
                         user_id=user_id, status=RunStatus.to_string(RunStatus.RUNNING),
                         start_time=start_time, end_time=None,
                         source_version="", lifecycle_stage=LifecycleStage.ACTIVE)

            tags_dict = {}
            for tag in tags:
                tags_dict[tag.key] = tag.value
            run.tags = [SqlTag(key=key, value=value) for key, value in tags_dict.items()]
            self._save_to_db(objs=run, session=session)

            return run.to_mlflow_entity()

    def _get_run(self, session, run_uuid, eager=False):
        """
        :param eager: If ``True``, eagerly loads the run's summary metrics (``latest_metrics``),
                      params, and tags when fetching the run. If ``False``, these attributes
                      are not eagerly loaded and will be loaded when their corresponding
                      object properties are accessed from the resulting ``SqlRun`` object.
        """
        query_options = self._get_eager_run_query_options() if eager else []
        runs = session \
            .query(SqlRun) \
            .options(*query_options) \
            .filter(SqlRun.run_uuid == run_uuid).all()

        if len(runs) == 0:
            raise MlflowException('Run with id={} not found'.format(run_uuid),
                                  RESOURCE_DOES_NOT_EXIST)
        if len(runs) > 1:
            raise MlflowException('Expected only 1 run with id={}. Found {}.'.format(run_uuid,
                                                                                     len(runs)),
                                  INVALID_STATE)

        return runs[0]

    @staticmethod
    def _get_eager_run_query_options():
        """
        :return: A list of SQLAlchemy query options that can be used to eagerly load the following
                 run attributes when fetching a run: ``latest_metrics``, ``params``, and ``tags``.
        """
        return [
            sqlalchemy.orm.joinedload(SqlRun.latest_metrics),
            sqlalchemy.orm.joinedload(SqlRun.params),
            sqlalchemy.orm.joinedload(SqlRun.tags)
        ]

    def _check_run_is_active(self, run):
        if run.lifecycle_stage != LifecycleStage.ACTIVE:
            raise MlflowException("The run {} must be in the 'active' state. Current state is {}."
                                  .format(run.run_uuid, run.lifecycle_stage),
                                  INVALID_PARAMETER_VALUE)

    def _check_experiment_is_active(self, experiment):
        if experiment.lifecycle_stage != LifecycleStage.ACTIVE:
            raise MlflowException("The experiment {} must be in the 'active' state. "
                                  "Current state is {}."
                                  .format(experiment.experiment_id, experiment.lifecycle_stage),
                                  INVALID_PARAMETER_VALUE)

    def _check_run_is_deleted(self, run):
        if run.lifecycle_stage != LifecycleStage.DELETED:
            raise MlflowException("The run {} must be in the 'deleted' state. Current state is {}."
                                  .format(run.run_uuid, run.lifecycle_stage),
                                  INVALID_PARAMETER_VALUE)

    def update_run_info(self, run_id, run_status, end_time):
        with self.ManagedSessionMaker() as session:
            run = self._get_run(run_uuid=run_id, session=session)
            self._check_run_is_active(run)
            run.status = RunStatus.to_string(run_status)
            run.end_time = end_time

            self._save_to_db(objs=run, session=session)
            run = run.to_mlflow_entity()

            return run.info

    def get_run(self, run_id):
        with self.ManagedSessionMaker() as session:
            # Load the run with the specified id and eagerly load its summary metrics, params, and
            # tags. These attributes are referenced during the invocation of
            # ``run.to_mlflow_entity()``, so eager loading helps avoid additional database queries
            # that are otherwise executed at attribute access time under a lazy loading model.
            run = self._get_run(run_uuid=run_id, session=session, eager=True)
            return run.to_mlflow_entity()

    def restore_run(self, run_id):
        with self.ManagedSessionMaker() as session:
            run = self._get_run(run_uuid=run_id, session=session)
            self._check_run_is_deleted(run)
            run.lifecycle_stage = LifecycleStage.ACTIVE
            self._save_to_db(objs=run, session=session)

    def delete_run(self, run_id):
        with self.ManagedSessionMaker() as session:
            run = self._get_run(run_uuid=run_id, session=session)
            self._check_run_is_active(run)
            run.lifecycle_stage = LifecycleStage.DELETED
            self._save_to_db(objs=run, session=session)

    def log_metric(self, run_id, metric):
        _validate_metric(metric.key, metric.value, metric.timestamp, metric.step)
        is_nan = math.isnan(metric.value)
        if is_nan:
            value = 0
        elif math.isinf(metric.value):
            #  NB: Sql can not represent Infs = > We replace +/- Inf with max/min 64b float value
            value = 1.7976931348623157e308 if metric.value > 0 else -1.7976931348623157e308
        else:
            value = metric.value
        with self.ManagedSessionMaker() as session:
            run = self._get_run(run_uuid=run_id, session=session)
            self._check_run_is_active(run)
            # ToDo: Consider prior checks for null, type, metric name validations, ... etc.
            logged_metric, just_created = self._get_or_create(
                model=SqlMetric, run_uuid=run_id, key=metric.key, value=value,
                timestamp=metric.timestamp, step=metric.step, session=session, is_nan=is_nan)
            # Conditionally update the ``latest_metrics`` table if the logged metric  was not
            # already present in the ``metrics`` table. If the logged metric was already present,
            # we assume that the ``latest_metrics`` table already accounts for its presence
            if just_created:
                self._update_latest_metric_if_necessary(logged_metric, session)

    @staticmethod
    def _update_latest_metric_if_necessary(logged_metric, session):
        def _compare_metrics(metric_a, metric_b):
            """
            :return: True if ``metric_a`` is strictly more recent than ``metric_b``, as determined
                     by ``step``, ``timestamp``, and ``value``. False otherwise.
            """
            return (metric_a.step, metric_a.timestamp, metric_a.value) > \
                   (metric_b.step, metric_b.timestamp, metric_b.value)

        # Fetch the latest metric value corresponding to the specified run_id and metric key and
        # lock its associated row for the remainder of the transaction in order to ensure
        # isolation
        latest_metric = session \
            .query(SqlLatestMetric) \
            .filter(
                SqlLatestMetric.run_uuid == logged_metric.run_uuid,
                SqlLatestMetric.key == logged_metric.key) \
            .with_for_update() \
            .one_or_none()
        if latest_metric is None or _compare_metrics(logged_metric, latest_metric):
            session.merge(
                SqlLatestMetric(
                    run_uuid=logged_metric.run_uuid, key=logged_metric.key,
                    value=logged_metric.value, timestamp=logged_metric.timestamp,
                    step=logged_metric.step, is_nan=logged_metric.is_nan))

    def get_metric_history(self, run_id, metric_key):
        with self.ManagedSessionMaker() as session:
            metrics = session.query(SqlMetric).filter_by(run_uuid=run_id, key=metric_key).all()
            return [metric.to_mlflow_entity() for metric in metrics]

    def log_param(self, run_id, param):
        with self.ManagedSessionMaker() as session:
            run = self._get_run(run_uuid=run_id, session=session)
            self._check_run_is_active(run)
            # if we try to update the value of an existing param this will fail
            # because it will try to create it with same run_uuid, param key
            try:
                # This will check for various integrity checks for params table.
                # ToDo: Consider prior checks for null, type, param name validations, ... etc.
                self._get_or_create(model=SqlParam, session=session, run_uuid=run_id,
                                    key=param.key, value=param.value)
                # Explicitly commit the session in order to catch potential integrity errors
                # while maintaining the current managed session scope ("commit" checks that
                # a transaction satisfies uniqueness constraints and throws integrity errors
                # when they are violated; "get_or_create()" does not perform these checks). It is
                # important that we maintain the same session scope because, in the case of
                # an integrity error, we want to examine the uniqueness of parameter values using
                # the same database state that the session uses during "commit". Creating a new
                # session synchronizes the state with the database. As a result, if the conflicting
                # parameter value were to be removed prior to the creation of a new session,
                # we would be unable to determine the cause of failure for the first session's
                # "commit" operation.
                session.commit()
            except sqlalchemy.exc.IntegrityError:
                # Roll back the current session to make it usable for further transactions. In the
                # event of an error during "commit", a rollback is required in order to continue
                # using the session. In this case, we re-use the session because the SqlRun, `run`,
                # is lazily evaluated during the invocation of `run.params`.
                session.rollback()
                existing_params = [p.value for p in run.params if p.key == param.key]
                if len(existing_params) > 0:
                    old_value = existing_params[0]
                    raise MlflowException(
                        "Changing param value is not allowed. Param with key='{}' was already"
                        " logged with value='{}' for run ID='{}. Attempted logging new value"
                        " '{}'.".format(
                            param.key, old_value, run_id, param.value), INVALID_PARAMETER_VALUE)
                else:
                    raise

    def set_experiment_tag(self, experiment_id, tag):
        """
        Set a tag for the specified experiment

        :param experiment_id: String ID of the experiment
        :param tag: ExperimentRunTag instance to log
        """
        _validate_experiment_tag(tag.key, tag.value)
        with self.ManagedSessionMaker() as session:
            experiment = self._get_experiment(session,
                                              experiment_id,
                                              ViewType.ALL).to_mlflow_entity()
            self._check_experiment_is_active(experiment)
            session.merge(SqlExperimentTag(experiment_id=experiment_id,
                                           key=tag.key,
                                           value=tag.value))

    def set_tag(self, run_id, tag):
        """
        Set a tag on a run.
        :param run_id: String ID of the run
        :param tag: RunTag instance to log
        """
        with self.ManagedSessionMaker() as session:
            _validate_tag(tag.key, tag.value)
            run = self._get_run(run_uuid=run_id, session=session)
            self._check_run_is_active(run)
            session.merge(SqlTag(run_uuid=run_id, key=tag.key, value=tag.value))

    def delete_tag(self, run_id, key):
        """
        Delete a tag from a run. This is irreversible.
        :param run_id: String ID of the run
        :param key: Name of the tag
        """
        with self.ManagedSessionMaker() as session:
            run = self._get_run(run_uuid=run_id, session=session)
            self._check_run_is_active(run)
            filtered_tags = session.query(SqlTag).filter_by(run_uuid=run_id, key=key).all()
            if len(filtered_tags) == 0:
                raise MlflowException(
                    "No tag with name: {} in run with id {}".format(key, run_id),
                    error_code=RESOURCE_DOES_NOT_EXIST)
            elif len(filtered_tags) > 1:
                raise MlflowException(
                    "Bad data in database - tags for a specific run must have "
                    "a single unique value."
                    "See https://mlflow.org/docs/latest/tracking.html#adding-tags-to-runs",
                    error_code=INVALID_STATE)
            session.delete(filtered_tags[0])

    def _search_runs(self, experiment_ids, filter_string, run_view_type, max_results, order_by,
                     page_token):
        # TODO: push search query into backend database layer
        if max_results > SEARCH_MAX_RESULTS_THRESHOLD:
            raise MlflowException("Invalid value for request parameter max_results. It must be at "
                                  "most {}, but got value {}".format(SEARCH_MAX_RESULTS_THRESHOLD,
                                                                     max_results),
                                  INVALID_PARAMETER_VALUE)

        stages = set(LifecycleStage.view_type_to_stages(run_view_type))
        with self.ManagedSessionMaker() as session:
            # Fetch the appropriate runs and eagerly load their summary metrics, params, and
            # tags. These run attributes are referenced during the invocation of
            # ``run.to_mlflow_entity()``, so eager loading helps avoid additional database queries
            # that are otherwise executed at attribute access time under a lazy loading model.
            queried_runs = session \
                .query(SqlRun) \
                .options(*self._get_eager_run_query_options()) \
                .filter(
                    SqlRun.experiment_id.in_(experiment_ids),
                    SqlRun.lifecycle_stage.in_(stages)) \
                .all()
            runs = [run.to_mlflow_entity() for run in queried_runs]

        filtered = SearchUtils.filter(runs, filter_string)
        sorted_runs = SearchUtils.sort(filtered, order_by)
        runs, next_page_token = SearchUtils.paginate(sorted_runs, page_token, max_results)
        return runs, next_page_token

    def log_batch(self, run_id, metrics, params, tags):
        _validate_run_id(run_id)
        _validate_batch_log_data(metrics, params, tags)
        _validate_batch_log_limits(metrics, params, tags)
        with self.ManagedSessionMaker() as session:
            run = self._get_run(run_uuid=run_id, session=session)
            self._check_run_is_active(run)
        try:
            for param in params:
                self.log_param(run_id, param)
            for metric in metrics:
                self.log_metric(run_id, metric)
            for tag in tags:
                self.set_tag(run_id, tag)
        except MlflowException as e:
            raise e
        except Exception as e:
            raise MlflowException(e, INTERNAL_ERROR)
