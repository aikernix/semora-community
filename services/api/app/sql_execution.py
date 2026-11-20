"""Sequential SQL execution and bounded, error-directed repair."""

import asyncio
import re
from semora_contracts import ExecutionResult, RunStatus
from semora_contracts.query_limits import EXECUTOR_HTTP_GRACE_SECONDS, EXPLAIN_TIMEOUT_DEFAULT_SECONDS
from .executor_errors import ExecutorOperationError
from .sql_errors import SQLIssue, issue_data, repairable
from .sql_dialect import compile_step, parse_query
from .result_engine import local_query


class SQLExecution:
    def __init__(self, workflow, run, ontology, settings):
        self.w, self.run, self.ontology, self.settings = workflow, run, ontology, settings

    def failed_step(self, step, exc, stage, *, attempts=1):
        artifact = self.run.source_artifacts.get(step.name)
        seconds = (self.settings.get('explain_timeout_seconds', EXPLAIN_TIMEOUT_DEFAULT_SECONDS) if stage == 'explain'
                   else self.settings.get('query_timeout_seconds', 30))
        data = {**issue_data(exc, stage), 'message': str(exc), 'step': step.name,
                'step_index': self.run.plan.sql_program.steps.index(step) + 1,
                'purpose': step.purpose, 'engine': step.engine,
                'data_source_id': step.data_source_id,
                'data_source_label': artifact.compilation.get('data_source_label', '') if artifact else '',
                'sql': artifact.physical_sql if artifact else step.sql,
                'dialect': artifact.dialect if artifact else step.dialect}
        if data.get('error_type') == 'timeout':
            message = ('单条SQL执行超过' + format(seconds, 'g') + '秒'
                       if data.get('timeout_origin') == 'system' else 'SQL执行超时')
            if stage == 'explain':
                message = f'数据库执行计划预检超时（{seconds:g} 秒）'
            data.update(message=message, timeout_seconds=seconds, retryable=False,
                        attempts=attempts, max_attempts=2 if stage=='execution' else 1, retries_remaining=0)
        self.run.agent_memory['execution_failure'] = [data]
        self.w.emit(self.run, 'source.step.failed', self.run.status, data, 'executor')

    def remember_rejection(self, step, artifact, error, stage):
        from .sql_feedback import record_sql_rejection
        try:
            entry = record_sql_rejection(step, artifact, error, stage)
        except Exception:
            self.run.delivery_warnings.append("本次SQL兼容性记录暂未保存，继续尝试定向修复。")
            return
        if entry:
            self.w.emit(self.run, "sql.compatibility.recorded", self.run.status,
                        {"step": step.name, "summary": "已记录数据库拒绝该SQL的原因，进入定向修复", **entry}, "executor")

    def artifact(self, step, results=None, *, template=False):
        import sqlglot
        from .sql_grain import validate_join_grain
        result_keys = {}
        for previous in self.run.plan.sql_program.steps:
            if previous.name == step.name:
                break
            objects = [o for o in self.ontology.get("objects", []) if previous.engine == "source" and
                (o.get("dataSourceId") or self.ontology.get("dataSourceId")) == previous.data_source_id]
            result_keys[previous.name] = validate_join_grain(parse_query(previous.sql, previous.dialect or "postgres"), objects, result_keys)
        value = compile_step(
            step,
            self.ontology,
            results,
            template=template,
            row_limit=int(self.settings.get("max_result_rows", 1000)),
            result_keys=result_keys,
        )
        if self.run.requirement:
            value.requirement_version = self.run.requirement.version
            value.requirement_hash = self.run.requirement.content_hash
        return value

    async def compile(self):
        self.run.source_artifacts = {}
        prior = {}
        for step in self.run.plan.sql_program.steps:
            while True:
                try:
                    artifact = self.artifact(step, prior, template=True)
                    break
                except SQLIssue as exc:
                    if not repairable(exc):
                        raise
                    from .sql_authoring import repair_step

                    await repair_step(self.w, self.run, step.name, issue_data(exc))
            self.run.source_artifacts[step.name] = artifact
            prior[step.name] = ExecutionResult(
                columns=step.output_columns, rows=[], row_count=0, duration_ms=0
            )
        self.run.sql = self.run.source_artifacts[self.run.plan.sql_program.steps[-1].name]
        self.run.sql_explanation = self.run.plan.sql_program.summary
        self.w.emit(
            self.run,
            "sql.ready",
            RunStatus.COMPILING,
            {"sql": self.run.sql.model_dump(mode="json")},
            "sql_author",
        )

    async def validate(self):
        for step in self.run.plan.sql_program.steps:
            artifact = self.run.source_artifacts[step.name]
            if step.engine == "local":
                artifact.compilation["validation"] = {
                    "state": "pending",
                    "method": "explain_after_inputs",
                }
                continue
            while True:
                error_stage = "baseline_validation"
                try:
                    artifact = self.artifact(step, template=True)
                    self.run.source_artifacts[step.name] = artifact
                    error_stage = "explain"
                    response = await self.w.estimate(
                        artifact.physical_sql, artifact.parameters, artifact.data_source_id,
                        self.settings.get("explain_timeout_seconds", EXPLAIN_TIMEOUT_DEFAULT_SECONDS),
                    )
                    if response.get("source") == "unavailable":
                        raise ExecutorOperationError("capability")
                    artifact.compilation["validation"] = {
                        "state": "template_validated" if step.inputs else "validated",
                        "method": "explain",
                    }
                    self.w.emit(
                        self.run,
                        "sql.validation.completed",
                        RunStatus.COMPILING,
                        {
                            "step": step.name,
                            "summary": "数据库已验证查询模板" if step.inputs else "数据库已验证SQL",
                        },
                        "executor",
                    )
                    break
                except (ExecutorOperationError, SQLIssue) as exc:
                    if isinstance(exc, ExecutorOperationError) and exc.kind == "capability":
                        artifact.compilation["validation"] = {
                            "state": "unavailable",
                            "method": "explain",
                        }
                        self.run.delivery_warnings.append(
                            step.name + "：数据库预校验能力不可用，将通过实际只读执行验证。"
                        )
                        break
                    if not repairable(exc):
                        raise
                    artifact.compilation["validation"] = {
                        "state": "invalid",
                        "error": issue_data(exc, error_stage),
                    }
                    self.remember_rejection(step, artifact, exc, error_stage)
                    from .sql_authoring import repair_step

                    await repair_step(self.w, self.run, step.name, issue_data(exc, error_stage))
        self.run.sql = self.run.source_artifacts[self.run.plan.sql_program.steps[-1].name]

    async def execute(self):
        results = {}
        from semora_agent.model_diagnostics import debug_payload

        for step in self.run.plan.sql_program.steps:
            self.w.emit(
                self.run,
                "source.step.started",
                RunStatus.EXECUTING,
                {"step": step.name, "summary": step.purpose},
                "executor",
            )
            revalidate = False
            timeout_retries = 0
            while True:
                error_stage = "baseline_validation"
                try:
                    artifact = self.artifact(step, results)
                    self.run.source_artifacts[step.name] = artifact
                    self.run.sql = artifact
                    debug_payload(
                        "sql.execute.request",
                        {
                            "step": step.name,
                            "sql": artifact.physical_sql,
                            "parameters": artifact.parameters,
                            "data_source_id": step.data_source_id,
                            "engine": step.engine,
                        },
                    )
                    if step.engine == "local":
                        error_stage = "execution"
                        try:
                            result = await asyncio.to_thread(
                                local_query, artifact, results, self.settings
                            )
                        except Exception as exc:
                            if isinstance(exc, SQLIssue):
                                raise
                            # Only binder/parser/conversion errors are SQL repairs.
                            import duckdb

                            if isinstance(
                                exc,
                                (
                                    duckdb.BinderException,
                                    duckdb.ParserException,
                                    duckdb.ConversionException,
                                    duckdb.CatalogException,
                                ),
                            ):
                                detail = re.sub(r"'(?:[^']|'')*'", "'<value>'", str(exc))
                                raise SQLIssue(detail[:1600]) from exc
                            raise
                    else:
                        if revalidate:
                            error_stage = "explain"
                            try:
                                await self.w.estimate(
                                    artifact.physical_sql, artifact.parameters, step.data_source_id,
                                    self.settings.get("explain_timeout_seconds", EXPLAIN_TIMEOUT_DEFAULT_SECONDS),
                                )
                            except ExecutorOperationError as exc:
                                if exc.kind != "capability":
                                    raise
                        error_stage = "execution"
                        result = await asyncio.wait_for(self.w.execute(
                            artifact.physical_sql,
                            artifact.parameters,
                            [],
                            {
                                **self.settings,
                                "data_source_id": step.data_source_id,
                                "execution_id": self.run.id,
                            },
                        ), timeout=float(self.settings.get('query_timeout_seconds', 30)) + EXECUTOR_HTTP_GRACE_SECONDS)
                    if result.columns != step.output_columns:
                        raise SQLIssue(
                            "实际输出列与步骤契约不一致：实际"
                            + str(result.columns)
                            + "；应为"
                            + str(step.output_columns)
                        )
                    if result.truncated and step.name != self.run.plan.sql_program.steps[-1].name:
                        raise SQLIssue(
                            "本步骤结果超过中间结果上限，不能完整传递给后续查询。"
                            "请保持确认口径，检查是否可将用户要求的排序和Top N、去重或聚合移到本源SQL中；"
                            "不能任意截取明细或缩小用户范围。当前上限："
                            + str(self.settings.get("max_result_rows", 1000))
                        )
                    break
                except Exception as exc:
                    if isinstance(exc, TimeoutError):
                        exc = ExecutorOperationError('timeout', diagnostic={'timeout_origin': 'system'})
                    if (error_stage == 'execution' and isinstance(exc, ExecutorOperationError)
                            and exc.kind == 'timeout' and timeout_retries < 1):
                        timeout_retries += 1
                        revalidate = False
                        self.w.emit(self.run, 'source.step.retry', RunStatus.EXECUTING, {
                            **exc.public(), 'step': step.name, 'attempt': 2, 'max_attempts': 2,
                            'retryable': True, 'retries_remaining': 1,
                            'timeout_seconds': float(self.settings.get('query_timeout_seconds', 30)),
                            'summary': '本条SQL执行超时，正在重试（第2次，共2次）',
                        }, 'executor')
                        continue
                    if not repairable(exc):
                        self.failed_step(step, exc, error_stage, attempts=timeout_retries + 1)
                        raise exc
                    if step.name in self.run.source_artifacts:
                        self.remember_rejection(step, self.run.source_artifacts[step.name], exc, error_stage)
                    if step.name in self.run.source_artifacts:
                        self.run.source_artifacts[step.name].compilation["validation"] = {
                            "state": "invalid",
                            "error": issue_data(exc, error_stage),
                        }
                    from .sql_authoring import repair_step

                    try:
                        await repair_step(self.w, self.run, step.name, issue_data(exc, error_stage))
                    except Exception as final:
                        self.failed_step(step, final, error_stage, attempts=timeout_retries + 1)
                        raise
                    revalidate = True
                    # The next iteration revalidates and executes the repaired
                    # statement. Earlier successful source queries are not replayed.
            artifact.compilation["validation"] = {
                "state": "validated",
                "method": "execution",
                "inputs_bound": True,
            }
            results[step.name] = result
            self.run.source_results[step.name] = result
            from .sql_learning import record_sql_success
            try:
                record_sql_success(step, result)
            except Exception:
                import logging
                logging.getLogger(__name__).warning("SQL capability learning was not saved")
            from .control_state import record_receipt
            from .run_state import digest

            receipt = {
                "step": step.name,
                "row_count": result.row_count,
                "duration_ms": result.duration_ms,
                "truncated": result.truncated,
                "output_hash": digest(result.model_dump(mode="json")),
            }
            record_receipt(self.run, receipt)
            self.w.emit(
                self.run,
                "source.step.completed",
                RunStatus.EXECUTING,
                {**receipt, "summary": step.purpose},
                "executor",
            )
            self.w.store.save_run(self.run)
        self.run.result = results[self.run.plan.sql_program.steps[-1].name]
        if self.run.result.truncated:
            self.run.delivery_warnings.append("最终结果达到返回上限，当前展示为截断结果。")
        self.w.emit(
            self.run,
            "execution.completed",
            RunStatus.EXECUTING,
            {"result": self.run.result.model_dump(mode="json")},
            "executor",
        )
