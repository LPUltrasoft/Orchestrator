"""Jenkins: jobs de cada proyecto, avisos después de cada push y estado de los builds.

Jenkins corre nativo, como el usuario `orc-ci` y con su Docker sin root, solo en
127.0.0.1 (decisión del usuario del 4/10/2026). GitHub no puede llamarlo (la PC no tiene
dirección pública), así que **el gateway le avisa** después de cada push: todos pasan por
él. Además cada job revisa GitHub cada 15 minutos, por si el usuario sube algo a mano.

Por proyecto: una carpeta con un job multibranch por repo (front y back), que baja de
GitHub con la credencial `github` que cargó el usuario y construye **solo `develop` y
`master`** (decisión del usuario del 5/10/2026: las ramas de tarea no van a Jenkins).
El token de API del usuario va en el `.env` (JENKINS_USER, JENKINS_TOKEN).
"""
from __future__ import annotations

import logging
from xml.sax.saxutils import escape

import httpx

from . import code, config

log = logging.getLogger("orchestrator.jenkins")


class JenkinsError(RuntimeError):
    pass


def configured() -> bool:
    return bool(config.JENKINS_URL and config.JENKINS_USER and config.JENKINS_TOKEN)


def _client() -> httpx.Client:
    # Con token de API no hace falta el crumb (protección CSRF de los formularios).
    return httpx.Client(base_url=config.JENKINS_URL, auth=(config.JENKINS_USER, config.JENKINS_TOKEN),
                        timeout=httpx.Timeout(60, connect=10))


def _job_path(project: str, repo: str | None = None, branch: str | None = None) -> str:
    path = f"/job/{code.slug(project)}"
    if repo:
        path += f"/job/{repo}"
    if branch:
        path += f"/job/{httpx.URL(branch).raw_path.decode()}"
    return path


_FOLDER = """<com.cloudbees.hudson.plugins.folder.Folder plugin="cloudbees-folder">
  <description>{description}</description>
</com.cloudbees.hudson.plugins.folder.Folder>"""

_MULTIBRANCH = """<org.jenkinsci.plugins.workflow.multibranch.WorkflowMultiBranchProject plugin="workflow-multibranch">
  <description>{description}</description>
  <orphanedItemStrategy class="com.cloudbees.hudson.plugins.folder.computed.DefaultOrphanedItemStrategy" plugin="cloudbees-folder">
    <pruneDeadBranches>true</pruneDeadBranches>
    <daysToKeep>-1</daysToKeep>
    <numToKeep>-1</numToKeep>
    <abortBuilds>false</abortBuilds>
  </orphanedItemStrategy>
  <triggers>
    <com.cloudbees.hudson.plugins.folder.computed.PeriodicFolderTrigger plugin="cloudbees-folder">
      <spec>H/15 * * * *</spec>
      <interval>900000</interval>
    </com.cloudbees.hudson.plugins.folder.computed.PeriodicFolderTrigger>
  </triggers>
  <sources class="jenkins.branch.MultiBranchProject$BranchSourceList" plugin="branch-api">
    <data>
      <jenkins.branch.BranchSource>
        <source class="jenkins.plugins.git.GitSCMSource" plugin="git">
          <id>orchestrator-{slug}-{repo}</id>
          <remote>{remote}</remote>
          <credentialsId>{credentials}</credentialsId>
          <traits>
            <jenkins.plugins.git.traits.BranchDiscoveryTrait/>
            <jenkins.scm.impl.trait.WildcardSCMHeadFilterTrait plugin="scm-api">
              <includes>{branches}</includes>
              <excludes></excludes>
            </jenkins.scm.impl.trait.WildcardSCMHeadFilterTrait>
          </traits>
        </source>
        <strategy class="jenkins.branch.DefaultBranchPropertyStrategy">
          <properties class="empty-list"/>
        </strategy>
      </jenkins.branch.BranchSource>
    </data>
    <owner class="org.jenkinsci.plugins.workflow.multibranch.WorkflowMultiBranchProject" reference="../.."/>
  </sources>
  <factory class="org.jenkinsci.plugins.workflow.multibranch.WorkflowBranchProjectFactory">
    <owner class="org.jenkinsci.plugins.workflow.multibranch.WorkflowMultiBranchProject" reference="../.."/>
    <scriptPath>Jenkinsfile</scriptPath>
  </factory>
</org.jenkinsci.plugins.workflow.multibranch.WorkflowMultiBranchProject>"""


def _exists(client: httpx.Client, path: str) -> bool:
    return client.get(f"{path}/api/json", params={"tree": "name"}).status_code == 200


def _create(client: httpx.Client, parent: str, name: str, xml: str) -> None:
    response = client.post(f"{parent}/createItem", params={"name": name}, content=xml.encode("utf-8"),
                           headers={"Content-Type": "application/xml"})
    if response.status_code >= 400:
        raise JenkinsError(f"no pude crear «{name}» en Jenkins: HTTP {response.status_code} {response.text[:300]}")


def ensure_jobs(project: str, repos: dict[str, dict]) -> list[str]:
    """La carpeta del proyecto y un job multibranch por repo. Los que ya existen se
    actualizan (por ejemplo, el filtro de ramas). Devuelve los creados o actualizados."""
    done = []
    slug = code.slug(project)
    with _client() as client:
        if not _exists(client, _job_path(project)):
            _create(client, "", slug, _FOLDER.format(description=escape(f"Proyecto «{project}» del Orchestrator")))
            done.append(slug)
        for repo, info in repos.items():
            xml = _MULTIBRANCH.format(
                description=escape(f"{repo} de «{project}»: GitHub {info['github']}"),
                slug=slug, repo=repo, remote=escape(f"https://github.com/{info['github']}.git"),
                credentials=escape(config.JENKINS_CREDENTIALS_ID),
                branches=escape(f"{config.DEV_BRANCH} {config.PROD_BRANCH}"),
            )
            if _exists(client, _job_path(project, repo)):
                response = client.post(f"{_job_path(project, repo)}/config.xml", content=xml.encode("utf-8"),
                                       headers={"Content-Type": "application/xml"})
                if response.status_code >= 400:
                    raise JenkinsError(f"no pude actualizar «{slug}/{repo}»: HTTP {response.status_code}")
                done.append(f"{slug}/{repo} (actualizado)")
            else:
                _create(client, _job_path(project), repo, xml)
                done.append(f"{slug}/{repo}")
    return done


def scan(project: str, repo: str) -> None:
    """Le avisa a Jenkins que hubo un push: escanea el repo y construye las ramas que cambiaron."""
    with _client() as client:
        response = client.post(f"{_job_path(project, repo)}/build", params={"delay": "0"})
        if response.status_code >= 400:
            raise JenkinsError(f"Jenkins no aceptó el escaneo de {repo}: HTTP {response.status_code}")


def branch_status(project: str, repo: str, branch: str) -> dict:
    """El último build de una rama. `{"job": False}` si la rama no tiene job (sin
    Jenkinsfile, o todavía no se escaneó)."""
    with _client() as client:
        response = client.get(f"{_job_path(project, repo, branch)}/lastBuild/api/json",
                              params={"tree": "number,result,building,url,timestamp,duration"})
        if response.status_code == 404:
            exists = _exists(client, _job_path(project, repo, branch))
            return {"job": exists, "build": None}
        response.raise_for_status()
        return {"job": True, "build": response.json()}


def last_number(project: str, repo: str, branch: str) -> int:
    """Número del último build de la rama (0 si no hay)."""
    status = branch_status(project, repo, branch)
    return (status.get("build") or {}).get("number") or 0


def wait_build(project: str, repo: str, branch: str, after: int, timeout: int = 1800) -> dict | None:
    """Espera un build nuevo (número > after) de la rama y que termine. None si no llega
    (por ejemplo, la rama no tiene Jenkinsfile)."""
    import time
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        time.sleep(15)
        status = branch_status(project, repo, branch)
        build = status.get("build")
        if build and build.get("number", 0) > after and not build.get("building"):
            return build
    return None


def console_tail(project: str, repo: str, branch: str, lines: int = 60) -> str:
    with _client() as client:
        response = client.get(f"{_job_path(project, repo, branch)}/lastBuild/consoleText")
        if response.status_code >= 400:
            return ""
        return "\n".join(response.text.splitlines()[-lines:])


def scan_log(project: str, repo: str, lines: int = 40) -> str:
    with _client() as client:
        response = client.get(f"{_job_path(project, repo)}/indexing/consoleText")
        return "\n".join(response.text.splitlines()[-lines:]) if response.status_code < 400 else ""


def whoami() -> str | None:
    """Para /health: con qué usuario entra el gateway, o None si Jenkins no responde."""
    try:
        with _client() as client:
            response = client.get("/me/api/json", params={"tree": "id"})
            return response.json().get("id") if response.status_code == 200 else None
    except httpx.HTTPError:
        return None
