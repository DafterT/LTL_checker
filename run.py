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
    # Достаем json файлы
    files = sorted(folder.glob("*.json"))  # Только указанная папка, без рекурсии.
    if not files:
        raise ValueError(f"В {folder} нет файлов *.json")

    tasks = []
    # Проходим по всем json файлам
    for path in files:
        try:
            # Загружаем json
            task = json.loads(path.read_text(encoding="utf-8-sig"))
            # Проверки для соответствия формату
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
            # Проверка тестов
            for test in task["tests"]:
                if not isinstance(test, dict):
                    raise ValueError("Каждый тест должен быть объектом")
                name = test.get("name", "") # Получаем имя теста
                if not isinstance(name, str) or not re.fullmatch(r"[a-zA-Z0-9_-]+", name):
                    raise ValueError("Имя теста: латинские буквы, цифры, _ или -")
                if name in names:
                    raise ValueError(f"Повторяется имя теста {name}")
                names.add(name) # Добавляем в список имен (тестов)
                if type(test.get("expected")) is not bool:
                    raise ValueError(f"{name}: expected должен быть true или false")
                # Проверка префиксов и цикла
                for part in ("prefix", "loop"):
                    # Если поля loop нету, пропускаем проверку
                    # Формат допускает опустить это поле
                    if part == "loop" and part not in test:
                        continue
                    states = test.get(part)
                    if not isinstance(states, list) or not states:
                        raise ValueError(f"{name}: {part} должен быть непустым списком")
                    # Проверка состояний
                    for state in states:
                        # Все предикаты имеют состояние
                        if not isinstance(state, dict) or set(state) != set(predicates):
                            raise ValueError(f"{name}: каждый элемент {part} должен содержать {predicates}")
                        # Состояния true или false
                        if any(type(v) not in (int, bool) or v not in (0, 1)
                               for v in state.values()):
                            raise ValueError(f"{name}: допустимы только 0, 1, false, true")
            task["id"] = path.stem # Имя файла без расширений
            task["source"] = str(path) # Путь к файлу
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
    # Начальные состояния
    declarations = "\n".join(f"bool {p} = {int(initial[p])};" for p in predicates)

    def step(state, label):
        # Задает нужные присваивания
        assignments = " ".join(f"{p} = {int(state[p])};" for p in predicates)
        # Предикаты для вывода
        values = ", ".join(f"{p}=%d" for p in predicates)
        # printf внутри d_step не добавляет отдельного момента времени.
        trace = f'printf("TRACE {label}: {values}\\n", {", ".join(predicates)});'
        # Итоговый шаг состоит из присваиваний и вывода
        return f"d_step {{ {assignments} {trace} }}"

    # Основное тело, последовательно проходит по каждому префиксу
    body = ["    " + step(state, f"prefix[{i}]")
            for i, state in enumerate(test["prefix"][1:], start=1)]
    # Обработка loop, если нету, все предикаты = 0
    loop = test.get("loop", [{p: 0 for p in predicates}])
    body.append("    do")
    for i, state in enumerate(loop):
        # Генерирует подпись
        label = f"loop[{i}]" if "loop" in test else "default_zero"
        # Генерация шага цикла
        body.append(("    :: " if i == 0 else "       ") + step(state, label))
    body.append("    od;")
    # Запись в шаблон
    return Template(template).substitute(
        declarations=declarations, body="\n".join(body), formula=task["formula"]
    )


def run_command(command, folder, log_name, timeout):
    """Запуск без shell; команда и весь вывод остаются в файле даже при ошибке."""
    # Преобразование листа в команду
    command_text = subprocess.list2cmdline(command) if os.name == "nt" else shlex.join(command)
    heading = f"$ {command_text}\n\n"
    try:
        # Запуск команды, весь вывод в result
        result = subprocess.run(
            command, cwd=folder, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
            text=True, encoding="utf-8", errors="replace", timeout=timeout,
        )
    except subprocess.TimeoutExpired as error: # Ошибка по времени
        output = error.stdout or b""
        if isinstance(output, bytes):
            output = output.decode("utf-8", errors="replace")
        # Записываем частичный вывод
        (folder / log_name).write_text(heading + output + "\nTIMEOUT\n", encoding="utf-8")
        # Запуск ошибки
        raise RuntimeError(f"Тайм-аут {timeout} с; см. {log_name}") from error
    except OSError as error: # Невозможность запуска (например нет прав)
        (folder / log_name).write_text(heading + str(error), encoding="utf-8")
        raise RuntimeError(f"Не удалось запустить {command[0]}; см. {log_name}") from error
    # Сохраняем вывод
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
    violation = r"^pan:\s*\d+:\s*(acceptance cycle\b|assertion violated\b|end state in claim\b)"
    if has_trail and re.search(violation, output, re.MULTILINE):
        return False
    raise RuntimeError("SPIN сообщил ошибку, не распознанную как нарушение LTL; см. verify.log")


def replay_trace(spin, folder, timeout):
    """Воспроизвести настоящий .trail и выделить из него состояния сценария."""
    # Загрузка трейса
    snapshot = json.loads((folder / "case.json").read_text(encoding="utf-8"))
    task, test = snapshot["task"], snapshot["test"]
    # Запуск команды
    log_path = (folder / "replay.log").resolve()
    result = run_command([spin, "-t", "-p", "-g", "model.pml"], folder, log_path, timeout)
    if result.returncode != 0 or "cannot find trail file" in result.stdout:
        raise RuntimeError(f"Не удалось воспроизвести контрпример; см. {log_path}")
    # Обработка
    # Выводим первую строку (её нет в .trail)
    lines = ["Контрпример из SPIN .trail:",
             f"t=0: {state_text(test['prefix'][0], task['predicates'])} (prefix[0])"]
    tick = 0
    # Выводим все строки
    for line in result.stdout.splitlines():
        if "START OF CYCLE" in line:
            lines.append("    --- начало повторяемого цикла SPIN ---")
        match = re.match(r"\s*TRACE ([^:]+): (.*)", line)
        if match:
            tick += 1
            lines.append(f"t={tick}: {match.group(2)} ({match.group(1)})")
    lines.append(f"Полные шаги модели и автомата LTL: {log_path}")
    trace = "\n".join(lines)
    (folder / "trace.txt").write_text(trace + "\n", encoding="utf-8")
    return trace


def run_test(task, test, template, folder, spin, cc, timeout, depth):
    """Сгенерировать, проверить, сравнить с ожиданием, сохранить контрпример."""
    folder.mkdir(parents=True)
    # Создаем модель по шаблону
    (folder / "model.pml").write_text(generate_model(task, test, template), encoding="utf-8")
    # Копирование конкретного теста
    (folder / "case.json").write_text(json.dumps(
        {"task": {k: task[k] for k in ("id", "formula", "predicates", "source")}, "test": test},
        ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    # Создание заготовки результата
    result = {"task": task, "test": test, "folder": folder,
              "status": "ERROR", "actual": None, "detail": "", "trace": ""}
    # Обработка теста
    try:
        executable = "pan.exe" if os.name == "nt" else "pan"
        # Генерации и компиляция
        commands = [
            ([spin, "-o3", "-a", "model.pml"], "generate.log"),
            ([cc, "-O2", "-DNOREDUCE", "-o", executable, "pan.c"], "compile.log"),
        ]
        for command, log in commands:
            process = run_command(command, folder, log, timeout)
            if process.returncode != 0:
                raise RuntimeError(f"Ошибка генерации или компиляции; см. {log}")
        # Запуск обработки
        process = run_command(
            [str(folder / executable), "-a", "-b", f"-m{depth}", "-w18"],
            folder, "verify.log", timeout,
        )
        actual = parse_result(process.stdout, process.returncode, (folder / "model.pml.trail").exists())
        result["actual"] = actual
        result["status"] = "PASS" if actual == test["expected"] else "FAIL"
        result["detail"] = "Формула выполняется" if actual else "Найден контрпример"
        if not actual:
            result["trace"] = replay_trace(spin, folder, timeout)
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
    # Парсим консоль
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--tasks", type=Path, default=ROOT / "tasks", help="Папка JSON-файлов")
    parser.add_argument("--test", default="*", help="Фильтр задание/тест, допускает *")
    parser.add_argument("--timeout", type=float, default=30, help="Лимит секунд на одну команду")
    parser.add_argument("--depth", type=int, default=10000, help="Максимальная глубина поиска")
    parser.add_argument("--replay", type=Path, help="Повторить контрпример из папки ранее выполненного теста")
    args = parser.parse_args()
    if args.timeout <= 0 or args.depth <= 0:
        parser.error("--timeout и --depth должны быть положительными")
    # Парсим конфиг
    try:
        config = json.loads((ROOT / "config.json").read_text(encoding="utf-8-sig"))
        if not isinstance(config, dict):
            raise ValueError("Ожидался JSON-объект")
    except (OSError, ValueError) as error:
        print(f"ERROR: Не удалось прочитать config.json: {error}", file=sys.stderr)
        return 2
    try:
        spin = os.path.expandvars(config["spin"])
        cc = os.path.expandvars(config["cc"])
    except (KeyError, TypeError) as error:
        missing = error.args[0] if isinstance(error, KeyError) else "spin или cc"
        print(f"ERROR: В config.json отсутствует или некорректен параметр {missing!r}", file=sys.stderr)
        return 2
    # Обновляем path (текущего процесса и дочерние)
    extra_path = [str(Path(tool).parent) for tool in (cc, spin) if Path(tool).is_absolute()]
    extra_path.extend(os.path.expandvars(path) for path in config.get("extra_path", []))
    os.environ["PATH"] = os.pathsep.join(extra_path + [os.environ.get("PATH", "")])
    # Обновляем пути через новый path
    spin = shutil.which(spin) or spin
    cc = shutil.which(cc) or cc

    # Запуск реплея
    if args.replay:
        try:
            print(replay_trace(spin, args.replay.resolve(), args.timeout))
            return 0
        except (RuntimeError, OSError, ValueError) as error:
            print(f"ERROR: {error}", file=sys.stderr)
            return 2

    # Запуск основных прогонов
    # Подготовка папок
    build = ROOT / "build"
    build.mkdir(exist_ok=True)
    run_folder = Path(tempfile.mkdtemp(prefix=datetime.now().strftime("%Y%m%d_%H%M%S_"), dir=build))
    report = run_folder / "report.txt"
    results, problem, environment = [], "", ""
    try:
        # Загрузили задания
        tasks = load_tasks(args.tasks)
        # Поиск выбранных тестов по шаблону из args.test
        selected = [(task, test.copy()) for task in tasks for test in task["tests"]
                    if fnmatch.fnmatchcase(f"{task['id']}/{test['name']}", args.test)]
        if not selected:
            raise ValueError(f"Нет тестов по фильтру {args.test!r}")
        # Загрузка шаблона
        template = (ROOT / "template.pml").read_text(encoding="utf-8")
        # Проверка SPIN
        version = run_command([spin, "-V"], run_folder, "environment.log", args.timeout)
        if version.returncode != 0:
            raise RuntimeError("SPIN не запускается. Проверьте путь в config.json")
        environment = version.stdout.strip()
        # Проверяем возможность перевода X
        probe = run_command([spin, "-f", "X probe"], run_folder, "next_support.log", args.timeout)
        if probe.returncode != 0:
            raise RuntimeError("Эта сборка SPIN не поддерживает X; нужна сборка с -DNXT.")
        # Проходим по каждому таску
        for task, test in selected:
            # Запуск теста
            result = run_test(task, test, template, run_folder / task["id"] / test["name"], spin, cc, args.timeout, args.depth)
            results.append(result)
            # Вывод результата теста
            print(f"[{result['status']}] {task['id']}/{test['name']}: {result['detail']}", flush=True)
            if result["status"] == "FAIL" and result["trace"]:
                print(result["trace"], flush=True)
    except (ValueError, OSError, RuntimeError) as error:
        problem = str(error)
        print(f"ERROR: {problem}", file=sys.stderr)
    # Запись общего репорта
    summary = write_report(results, report, run_folder, environment, problem)
    # Вывод итогов
    print(summary)
    print(f"Отчёт: {report.resolve()}")
    if problem or any(r["status"] == "ERROR" for r in results):
        return 2
    return 1 if any(r["status"] == "FAIL" for r in results) else 0


if __name__ == "__main__":
    sys.exit(main())
