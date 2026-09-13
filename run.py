#!/usr/bin/env python3
"""Проверка LTL на заданных последовательностях: python run.py.

Только стандартная библиотека. Используется уже установленное окружение.
Основной маршрут: load_tasks → generate_model → run_test → parse_result
→ write_report. Для каждого теста сохраняются модель, команды и контрпример.
"""

import argparse
import fnmatch
import json
import os
from pathlib import Path
import re
import shlex
import shutil
from string import Template
import subprocess
import sys
import tempfile
from datetime import datetime


ROOT = Path(__file__).resolve().parent


def load_tasks(folder):
    """Прочитать JSON-файлы и проверить, что состояния заданы однозначно."""
    files = sorted(folder.glob("*.json"))  # Только указанная папка, без рекурсии.
    if not files:
        raise ValueError(f"В {folder} нет файлов *.json")

    tasks = []
    for path in files:
        try:
            task = json.loads(path.read_text(encoding="utf-8-sig"))
            if not isinstance(task, dict):
                raise ValueError("Ожидался JSON-объект")
            predicates = task.get("predicates")
            if not isinstance(predicates, list) or not predicates:
                raise ValueError("predicates должен быть непустым списком имён")
            if any(not isinstance(p, str) or not re.fullmatch(r"[a-z][a-z0-9_]*", p)
                   for p in predicates):
                raise ValueError("Имена предикатов: строчные латинские буквы, цифры, _")
            if len(set(predicates)) != len(predicates):
                raise ValueError("Имена предикатов не должны повторяться")
            if not isinstance(task.get("formula"), str) or not task["formula"].strip():
                raise ValueError("Нужна непустая строка formula")
            if not isinstance(task.get("tests"), list) or not task["tests"]:
                raise ValueError("Нужен непустой список tests")

            names = set()
            for test in task["tests"]:
                if not isinstance(test, dict):
                    raise ValueError("Каждый тест должен быть объектом")
                name = test.get("name", "")
                if not isinstance(name, str) or not re.fullmatch(r"[a-zA-Z0-9_-]+", name):
                    raise ValueError("Имя теста: латинские буквы, цифры, _ или -")
                if name in names:
                    raise ValueError(f"Повторяется имя теста {name}")
                names.add(name)
                if type(test.get("expected")) is not bool:
                    raise ValueError(f"{name}: expected должен быть true или false")
                for part in ("prefix", "loop"):
                    if part == "loop" and part not in test:
                        continue
                    states = test.get(part)
                    if not isinstance(states, list) or not states:
                        raise ValueError(f"{name}: {part} должен быть непустым списком")
                    for state in states:
                        if not isinstance(state, dict) or set(state) != set(predicates):
                            raise ValueError(f"{name}: каждый элемент {part} должен содержать {predicates}")
                        if any(type(v) not in (int, bool) or v not in (0, 1)
                               for v in state.values()):
                            raise ValueError(f"{name}: допустимы только 0, 1, false, true")
            task["id"] = path.stem
            task["source"] = str(path)
            tasks.append(task)
        except (ValueError, TypeError) as error:
            raise ValueError(f"{path.name}: {error}") from error
    return tasks


def state_text(state, predicates):
    return ", ".join(f"{p}={int(state[p])}" for p in predicates)


def generate_model(task, test, template):
    """Один элемент последовательности = один d_step, включая вывод для отладки."""
    predicates = task["predicates"]
    initial = test["prefix"][0]
    declarations = "\n".join(f"bool {p} = {int(initial[p])};" for p in predicates)

    def step(state, label):
        assignments = " ".join(f"{p} = {int(state[p])};" for p in predicates)
        values = ", ".join(f"{p}=%d" for p in predicates)
        # printf внутри d_step не добавляет отдельного момента времени.
        trace = f'printf("TRACE {label}: {values}\\n", {", ".join(predicates)});'
        return f"d_step {{ {assignments} {trace} }}"

    body = ["    " + step(state, f"prefix[{i}]")
            for i, state in enumerate(test["prefix"][1:], start=1)]
    # Без loop события заканчиваются: со следующего момента все предикаты ложны.
    loop = test.get("loop", [{p: 0 for p in predicates}])
    body.append("    do")
    for i, state in enumerate(loop):
        label = f"loop[{i}]" if "loop" in test else "default_zero"
        body.append(("    :: " if i == 0 else "       ") + step(state, label))
    body.append("    od;")
    return Template(template).substitute(
        declarations=declarations, body="\n".join(body), formula=task["formula"]
    )


def run_command(command, folder, log_name, timeout):
    """Запуск без shell; команда и весь вывод остаются в файле даже при ошибке."""
    command_text = subprocess.list2cmdline(command) if os.name == "nt" else shlex.join(command)
    heading = f"$ {command_text}\n\n"
    try:
        result = subprocess.run(
            command, cwd=folder, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
            text=True, encoding="utf-8", errors="replace", timeout=timeout,
        )
    except subprocess.TimeoutExpired as error:
        output = error.stdout or b""
        if isinstance(output, bytes):
            output = output.decode("utf-8", errors="replace")
        (folder / log_name).write_text(heading + output + "\nTIMEOUT\n", encoding="utf-8")
        raise RuntimeError(f"Тайм-аут {timeout} с; см. {log_name}") from error
    except OSError as error:
        (folder / log_name).write_text(heading + str(error), encoding="utf-8")
        raise RuntimeError(f"Не удалось запустить {command[0]}; см. {log_name}") from error
    (folder / log_name).write_text(heading + result.stdout, encoding="utf-8")
    return result


def parse_result(output, returncode, has_trail):
    """Вернуть True/False для формулы; технические сбои не являются контрпримером."""
    if returncode != 0:
        raise RuntimeError(f"Верификатор завершился с кодом {returncode}; см. verify.log")
    limits = ("max search depth too small", "depth limit reached", "out of memory",
              "VECTORSZ is too small", "cannot allocate", "time limit reached")
    if any(message.lower() in output.lower() for message in limits):
        raise RuntimeError("Проверка ограничена ресурсами или глубиной; см. verify.log")
    match = re.search(r"\berrors:\s*(\d+)", output)
    if not match:
        raise RuntimeError("Нет итоговой строки errors: N; см. verify.log")
    if "Full statespace search for:" not in output or re.search(r"\+\s+Partial Order Reduction", output):
        raise RuntimeError("Нужен полный поиск без сокращения частичных порядков; см. verify.log")
    if not re.search(r"never claim\s+\+", output):
        raise RuntimeError("LTL-проверка не включена; см. verify.log")
    if not re.search(r"acceptance\s+cycles\s+\+", output):
        raise RuntimeError("Поиск принимающих циклов не включён; см. verify.log")
    if int(match.group(1)) == 0:
        if "search not completed" in output.lower():
            raise RuntimeError("Поиск не завершён; см. verify.log")
        return True
    # В генерируемой модели нет пользовательских assert. Здесь assertion violated
    # относится к автомату отрицания LTL, который SPIN добавил в модель.
    violation = r"^pan:\s*\d+:\s*(acceptance cycle\b|assertion violated\b|end state in claim\b)"
    if has_trail and re.search(violation, output, re.MULTILINE):
        return False
    raise RuntimeError("SPIN сообщил ошибку, не распознанную как нарушение LTL; см. verify.log")


def replay_trace(spin, folder, timeout):
    """Воспроизвести настоящий .trail и выделить из него состояния сценария."""
    snapshot = json.loads((folder / "case.json").read_text(encoding="utf-8"))
    task, test = snapshot["task"], snapshot["test"]
    result = run_command([spin, "-t", "-p", "-g", "model.pml"], folder, "replay.log", timeout)
    if result.returncode != 0 or "cannot find trail file" in result.stdout:
        raise RuntimeError("Не удалось воспроизвести контрпример; см. replay.log")
    lines = ["Контрпример из SPIN .trail:",
             f"t=0: {state_text(test['prefix'][0], task['predicates'])} (prefix[0], начальное)"]
    tick = 0
    for line in result.stdout.splitlines():
        if "START OF CYCLE" in line:
            lines.append("    --- начало повторяемого цикла SPIN ---")
        match = re.match(r"\s*TRACE ([^:]+): (.*)", line)
        if match:
            tick += 1
            lines.append(f"t={tick}: {match.group(2)} ({match.group(1)})")
    lines.append("Полные шаги модели и автомата LTL: replay.log")
    trace = "\n".join(lines)
    (folder / "trace.txt").write_text(trace + "\n", encoding="utf-8")
    return trace


def run_test(task, test, template, folder, args):
    """Сгенерировать, проверить, сравнить с ожиданием, сохранить контрпример."""
    folder.mkdir(parents=True)
    (folder / "model.pml").write_text(generate_model(task, test, template), encoding="utf-8")
    (folder / "case.json").write_text(json.dumps(
        {"task": {k: task[k] for k in ("id", "formula", "predicates", "source")}, "test": test},
        ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    result = {"task": task, "test": test, "folder": folder,
              "status": "ERROR", "actual": None, "detail": "", "trace": ""}
    try:
        executable = "pan.exe" if os.name == "nt" else "pan"
        commands = [
            ([args.spin, "-o3", "-a", "model.pml"], "generate.log"),
            ([args.cc, "-O2", "-DNOREDUCE", "-o", executable, "pan.c"], "compile.log"),
        ]
        for command, log in commands:
            process = run_command(command, folder, log, args.timeout)
            if process.returncode != 0:
                raise RuntimeError(f"Ошибка генерации или компиляции; см. {log}")
        process = run_command(
            [str(folder / executable), "-a", "-b", f"-m{args.depth}", "-w18"],
            folder, "verify.log", args.timeout,
        )
        actual = parse_result(process.stdout, process.returncode, (folder / "model.pml.trail").exists())
        result["actual"] = actual
        result["status"] = "PASS" if actual == test["expected"] else "FAIL"
        result["detail"] = "Формула выполняется" if actual else "Найден контрпример"
        if not actual:
            result["trace"] = replay_trace(args.spin, folder, args.timeout)
    except RuntimeError as error:
        result["status"] = "ERROR"
        result["detail"] = str(error)
    return result


def write_report(results, report, run_folder, environment, problem=""):
    """Записать читаемый отчёт: ожидание, факт, статус, трасса и файлы отладки."""
    lines = ["ПРОВЕРКА LTL С ПОМОЩЬЮ SPIN", f"Дата: {datetime.now():%Y-%m-%d %H:%M:%S}",
             f"Файлы запуска: {run_folder}", environment,
             "Настройки: spin -o3; gcc -DNOREDUCE; pan -a -b (полный поиск)",
             "PASS = УСПЕХ; FAIL = НЕУДАЧА (ожидание не совпало); ERROR = НЕУДАЧА (техническая ошибка).",
             "Без loop после prefix все предикаты равны 0 бесконечно.", ""]
    if problem:
        lines.extend(["ERROR: " + problem, ""])
    for result in results:
        task, test = result["task"], result["test"]
        lines.extend([
            f"[{result['status']}] {task['id']}/{test['name']}",
            f"  Формула: {task['formula']}",
            f"  Ожидалось: {str(test['expected']).lower()}",
            f"  Получено: {str(result['actual']).lower() if result['actual'] is not None else 'не определено'}",
            f"  Причина: {result['detail']}",
        ])
        if test.get("note"):
            lines.append(f"  Смысл теста: {test['note']}")
        if "original_expected" in test:
            lines.append(f"  Ожидание переопределено через --expect; в исходном JSON: {str(test['original_expected']).lower()}")
        for part in ("prefix", "loop"):
            if part in test:
                sequence = " -> ".join("(" + state_text(s, task["predicates"]) + ")" for s in test[part])
                lines.append(f"  {part}: {sequence}" + ("; повторять бесконечно" if part == "loop" else ""))
        if "loop" not in test:
            zeros = state_text({p: 0 for p in task["predicates"]}, task["predicates"])
            lines.append(f"  loop (по умолчанию): ({zeros}); повторять бесконечно")
        if result["trace"]:
            lines.extend("  " + line for line in result["trace"].splitlines())
        lines.extend([f"  Файлы: {result['folder']}", ""])
    counts = {status: sum(r["status"] == status for r in results) for status in ("PASS", "FAIL", "ERROR")}
    lines.append(f"Итого: {len(results)} тестов; PASS={counts['PASS']}, FAIL={counts['FAIL']}, ERROR={counts['ERROR']}.")
    if problem:
        lines.append("Запуск завершился с общей ошибкой.")
    report.parent.mkdir(parents=True, exist_ok=True)
    report.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return lines[-1]


def main():
    # В Windows stdout без этого может использовать cp1251 вместо UTF-8.
    sys.stdout.reconfigure(encoding="utf-8")
    sys.stderr.reconfigure(encoding="utf-8")
    config_error = ""
    try:
        config = json.loads((ROOT / "config.json").read_text(encoding="utf-8-sig"))
        if not isinstance(config, dict):
            raise ValueError("Ожидался JSON-объект")
    except (OSError, ValueError) as error:
        config, config_error = {}, f"Не удалось прочитать config.json: {error}"
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--tasks", type=Path, default=ROOT / "tasks", help="Папка JSON-файлов")
    parser.add_argument("--test", default="*", help="Фильтр задание/тест, допускает *")
    parser.add_argument("--expect", choices=("true", "false"), help="Переопределить ожидание для одного выбранного теста")
    parser.add_argument("--report", type=Path, help="Имя текстового отчёта внутри папки текущего билда")
    parser.add_argument("--spin", default=config.get("spin", "spin"), help="Путь к SPIN с поддержкой X")
    parser.add_argument("--cc", default=config.get("cc", "gcc"), help="C-компилятор")
    parser.add_argument("--timeout", type=float, default=30, help="Лимит секунд на одну команду")
    parser.add_argument("--depth", type=int, default=10000, help="Максимальная глубина поиска")
    parser.add_argument("--replay", type=Path, help="Повторить контрпример из папки ранее выполненного теста")
    args = parser.parse_args()
    if args.timeout <= 0 or args.depth <= 0:
        parser.error("--timeout и --depth должны быть положительными")
    args.spin = os.path.expandvars(args.spin)
    args.cc = os.path.expandvars(args.cc)
    # Только PATH текущего Python-процесса и его дочерних команд.
    # SPIN сам вызывает препроцессор, которому тоже нужен доступ к gcc.
    extra_path = [str(Path(tool).parent) for tool in (args.cc, args.spin) if Path(tool).is_absolute()]
    extra_path.extend(os.path.expandvars(path) for path in config.get("extra_path", []))
    os.environ["PATH"] = os.pathsep.join(extra_path + [os.environ.get("PATH", "")])
    args.spin = shutil.which(args.spin) or args.spin
    args.cc = shutil.which(args.cc) or args.cc
    if args.replay:
        try:
            print(replay_trace(args.spin, args.replay.resolve(), args.timeout))
            return 0
        except (RuntimeError, OSError, ValueError) as error:
            print(f"ERROR: {error}", file=sys.stderr)
            return 2

    build = ROOT / "build"
    build.mkdir(exist_ok=True)
    # Новая папка на каждый запуск: старые .trail и pan не могут подменить результат.
    run_folder = Path(tempfile.mkdtemp(prefix=datetime.now().strftime("%Y%m%d_%H%M%S_"), dir=build))
    # Имя без пути сохраняется рядом с артефактами именно этого запуска.
    # Явный абсолютный путь пользователя не переопределяем.
    if args.report is None:
        args.report = run_folder / "report.txt"
    elif not args.report.is_absolute():
        args.report = run_folder / args.report
    results, problem, environment = [], "", ""
    try:
        if config_error:
            raise ValueError(config_error)
        tasks = load_tasks(args.tasks)
        selected = [(task, test.copy()) for task in tasks for test in task["tests"]
                    if fnmatch.fnmatchcase(f"{task['id']}/{test['name']}", args.test)]
        if not selected:
            raise ValueError(f"Нет тестов по фильтру {args.test!r}")
        if args.expect:
            if len(selected) != 1:
                raise ValueError("--expect требует ровно один выбранный тест (--test задание/тест)")
            selected[0][1]["original_expected"] = selected[0][1]["expected"]
            selected[0][1]["expected"] = args.expect == "true"
        template = (ROOT / "template.pml").read_text(encoding="utf-8")
        version = run_command([args.spin, "-V"], run_folder, "environment.log", args.timeout)
        if version.returncode != 0:
            raise RuntimeError("SPIN не запускается. Проверьте путь в config.json")
        environment = version.stdout.strip()
        # Проверяем возможность перевода X, а не предполагаем её по номеру версии.
        probe = run_command([args.spin, "-f", "X probe"], run_folder, "next_support.log", args.timeout)
        if probe.returncode != 0:
            raise RuntimeError("Эта сборка SPIN не поддерживает X; нужна сборка с -DNXT. Окружение не изменено.")
        for task, test in selected:
            result = run_test(task, test, template, run_folder / task["id"] / test["name"], args)
            results.append(result)
            print(f"[{result['status']}] {task['id']}/{test['name']}: {result['detail']}", flush=True)
            if result["status"] == "FAIL" and result["trace"]:
                print(result["trace"], flush=True)
    except (ValueError, OSError, RuntimeError) as error:
        problem = str(error)
        print(f"ERROR: {problem}", file=sys.stderr)
    summary = write_report(results, args.report, run_folder, environment, problem)
    print(summary)
    print(f"Отчёт: {args.report.resolve()}")
    if problem or any(r["status"] == "ERROR" for r in results):
        return 2
    return 1 if any(r["status"] == "FAIL" for r in results) else 0


if __name__ == "__main__":
    sys.exit(main())
