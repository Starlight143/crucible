# ruff: noqa: E402
"""Tests for K8s/Helm additions in deployment_artifacts.py."""
import json
import os
import sys
import tempfile
import unittest

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

from crucible.features.deployment_artifacts import (
    _generate_helm_chart,
    _generate_helm_deployment_template,
    _generate_helm_service_template,
    _generate_helm_values,
    _generate_k8s_deployment,
    _generate_k8s_service,
    generate_deployment_artifacts,
)


class TestGenerateK8sDeployment(unittest.TestCase):
    def test_contains_deployment_kind(self) -> None:
        yaml = _generate_k8s_deployment("myapp", "fastapi", "3.11")
        self.assertIn("kind: Deployment", yaml)
        self.assertIn("name: myapp", yaml)
        self.assertIn("containerPort: 8000", yaml)

    def test_no_port_framework(self) -> None:
        yaml = _generate_k8s_deployment("quantbot", "quant", "3.11")
        # An empty `ports:` key without items is invalid K8s YAML;
        # the generator must omit the entire block for port-less frameworks.
        self.assertNotIn("ports:", yaml)
        self.assertNotIn("containerPort:", yaml)
        self.assertNotIn("readinessProbe", yaml)

    def test_resources_present(self) -> None:
        yaml = _generate_k8s_deployment("app", "flask", "3.11")
        self.assertIn("resources:", yaml)
        self.assertIn("cpu: 100m", yaml)
        self.assertIn("memory: 512Mi", yaml)


class TestGenerateK8sService(unittest.TestCase):
    def test_contains_service_kind(self) -> None:
        yaml = _generate_k8s_service("myapp", "fastapi")
        self.assertIn("kind: Service", yaml)
        self.assertIn("name: myapp", yaml)
        self.assertIn("targetPort: 8000", yaml)
        self.assertIn("port: 80", yaml)

    def test_no_port_headless_has_no_port_mapping(self) -> None:
        # For headless frameworks (quant), the Service must NOT create a
        # port mapping to 8000 — nothing is listening on that port.
        yaml = _generate_k8s_service("quantbot", "quant")
        self.assertIn("kind: Service", yaml)
        self.assertNotIn("targetPort:", yaml)
        self.assertNotIn("port: 80", yaml)
        # Selector must still be present for pod discovery
        self.assertIn("app: quantbot", yaml)


class TestGenerateHelmChart(unittest.TestCase):
    def test_chart_yaml(self) -> None:
        yaml = _generate_helm_chart("myapp")
        self.assertIn("apiVersion: v2", yaml)
        self.assertIn("name: myapp", yaml)
        self.assertIn("version: 0.1.0", yaml)


class TestGenerateHelmValues(unittest.TestCase):
    def test_values_yaml(self) -> None:
        yaml = _generate_helm_values("myapp", "fastapi")
        self.assertIn("replicaCount: 2", yaml)
        self.assertIn("repository: myapp", yaml)
        self.assertIn("targetPort: 8000", yaml)
        self.assertIn("healthCheck:", yaml)


class TestGenerateHelmTemplates(unittest.TestCase):
    def test_deployment_template(self) -> None:
        # Web framework: readinessProbe must be present
        yaml = _generate_helm_deployment_template("myapp", "fastapi")
        self.assertIn("kind: Deployment", yaml)
        self.assertIn(".Values.replicaCount", yaml)
        self.assertIn(".Values.image.repository", yaml)
        self.assertIn("readinessProbe", yaml)

    def test_deployment_template_headless_no_readiness_probe(self) -> None:
        # Headless framework: readinessProbe would always fail (no HTTP server).
        yaml = _generate_helm_deployment_template("quantbot", "quant")
        self.assertIn("kind: Deployment", yaml)
        self.assertNotIn("readinessProbe", yaml)
        self.assertNotIn("containerPort:", yaml)

    def test_service_template(self) -> None:
        yaml = _generate_helm_service_template("myapp")
        self.assertIn("kind: Service", yaml)
        self.assertIn(".Values.service.type", yaml)
        self.assertIn(".Values.service.port", yaml)

    def test_service_template_guarded_by_enabled(self) -> None:
        """
        Regression (v16.0.10): service.yaml was missing the
        {{- if .Values.service.enabled }} guard, causing Helm render failures
        for headless deployments where service.type/port/targetPort are absent
        from values.yaml.  The guard must be present.
        """
        yaml = _generate_helm_service_template("myapp")
        self.assertIn("if .Values.service.enabled", yaml,
                      "service.yaml must guard on .Values.service.enabled")
        self.assertIn("{{- end }}", yaml,
                      "service.yaml if-block must be closed with {{- end }}")

    def test_helm_values_headless_has_enabled_false_and_no_port_fields(self) -> None:
        """
        Regression (v16.0.10): headless values.yaml must have service.enabled: false
        and must NOT define service.type/port/targetPort (those fields are only
        safe to reference when service.enabled: true).
        """
        yaml = _generate_helm_values("quantbot", "quant")  # quant = headless
        self.assertIn("enabled: false", yaml)
        self.assertNotIn("targetPort:", yaml)
        self.assertNotIn("service:\n  type:", yaml)

    def test_helm_values_web_has_enabled_true(self) -> None:
        """
        Regression (v16.0.10): web-framework values.yaml must include
        service.enabled: true so the {{- if .Values.service.enabled }} guard
        in service.yaml renders the Service resource correctly.
        """
        yaml = _generate_helm_values("webapi", "fastapi")
        self.assertIn("enabled: true", yaml,
                      "Web-framework values.yaml must set service.enabled: true")


class TestGenerateDeploymentArtifactsWithK8s(unittest.TestCase):
    def test_k8s_and_helm_artifacts_generated(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            code_dir = os.path.join(td, "code")
            os.makedirs(code_dir)
            with open(os.path.join(code_dir, "main.py"), "w") as f:
                f.write("from fastapi import FastAPI\napp = FastAPI()\n")
            report = generate_deployment_artifacts(td)
            self.assertTrue(report.success)
            # Check K8s artifacts
            k8s_artifacts = [a for a in report.artifacts_generated if "k8s" in a]
            self.assertGreater(len(k8s_artifacts), 0)
            # Check Helm artifacts
            helm_artifacts = [a for a in report.artifacts_generated if "helm" in a]
            self.assertGreater(len(helm_artifacts), 0)
            # Verify files exist
            deploy_dir = os.path.join(td, "deployment")
            self.assertTrue(os.path.isfile(os.path.join(deploy_dir, "k8s", "deployment.yaml")))
            self.assertTrue(os.path.isfile(os.path.join(deploy_dir, "k8s", "service.yaml")))
            self.assertTrue(os.path.isfile(os.path.join(deploy_dir, "helm", "Chart.yaml")))
            self.assertTrue(os.path.isfile(os.path.join(deploy_dir, "helm", "values.yaml")))
            self.assertTrue(os.path.isfile(os.path.join(
                deploy_dir, "helm", "templates", "deployment.yaml")))
            self.assertTrue(os.path.isfile(os.path.join(
                deploy_dir, "helm", "templates", "service.yaml")))

    def test_project_name_sanitisation(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            code_dir = os.path.join(td, "code")
            os.makedirs(code_dir)
            # Write a run_meta with a name that needs sanitisation
            with open(os.path.join(td, "run_meta.json"), "w") as f:
                json.dump({"project_name": "My Special Project!"}, f)
            with open(os.path.join(code_dir, "main.py"), "w") as f:
                f.write("print('hello')\n")
            report = generate_deployment_artifacts(td)
            self.assertTrue(report.success)
            # K8s deployment should have sanitised name
            k8s_dep = os.path.join(td, "deployment", "k8s", "deployment.yaml")
            with open(k8s_dep) as f:
                content = f.read()
            self.assertIn("my-special-project-", content)


if __name__ == "__main__":
    unittest.main()
