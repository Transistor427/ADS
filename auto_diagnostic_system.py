# Auto Diagnostic System for Klipper
#
# This file may be distributed under the terms of the GNU GPLv3 license.

import logging

HEATER_NAMES = ('heater_bed', 'extruder', 'chamber')
OK = "Успешно"


class AutoDiagnostic:
    def __init__(self, config):
        self.printer = config.get_printer()
        self.gcode = self.printer.lookup_object('gcode')
        self.reactor = self.printer.get_reactor()
        self.results = {}
        self.temps = {
            'heater_bed': config.getfloat('bed_temp', 60.0, minval=0),
            'extruder': config.getfloat('extruder_temp', 200.0),
            'chamber': config.getfloat('chamber_temp', 0.0, minval=0),
        }
        self.park = {
            'x': config.getfloat('park_x', 150.0),
            'y': config.getfloat('park_y', 150.0),
            'z': config.getfloat('park_z', 100.0),
        }
        self.fan_check_drop = config.getfloat('fan_temp_drop', 2.0, minval=0.5)
        self.fan_check_time = config.getfloat('fan_check_time', 15.0, minval=1.0)
        self.stabilization_time = config.getfloat('stabilization_time', 5.0, minval=0)
        self.gcode.register_command(
            'AUTO_DIAGNOSTIC',
            self.cmd_AUTO_DIAGNOSTIC,
            desc=self.cmd_AUTO_DIAGNOSTIC_help,
        )

    cmd_AUTO_DIAGNOSTIC_help = "Запуск автоматической диагностики принтера"

    def cmd_AUTO_DIAGNOSTIC(self, gcmd):
        self.results = {
            'heaters': {},
            'parking': {},
            'leveling': {},
            'fan_check': None,
            'skip_leveling': False,
        }
        try:
            self.gcode.respond_info("=== ЗАПУСК АВТОДИАГНОСТИКИ ===")
            self._heaters_check()
            self._park_axes()
            self._fan_check()
            if not self.results['skip_leveling']:
                self._bed_leveling()
        except Exception as e:
            logging.exception("Diagnostic failed")
            self.gcode.respond_info(f"Критическая ошибка: {e}")
        finally:
            self._finalize()

    def _lookup_heater(self, name):
        try:
            heaters = self.printer.lookup_object('heaters')
            return heaters.lookup_heater(name)
        except Exception:
            pass
        try:
            obj = self.printer.lookup_object(name)
        except Exception:
            return None
        if hasattr(obj, 'get_temp'):
            return obj
        if hasattr(obj, 'heater'):
            return obj.heater
        return None

    def _get_temp(self, heater_or_name):
        heater = (
            self._lookup_heater(heater_or_name)
            if isinstance(heater_or_name, str)
            else heater_or_name
        )
        if heater is None:
            return None
        try:
            if hasattr(heater, 'get_temp'):
                temp, _ = heater.get_temp(self.reactor.monotonic())
                return temp
            status = heater.get_status(self.reactor.monotonic())
            return status.get('temperature')
        except Exception:
            return None

    def _set_heater(self, name, target):
        self.gcode.run_script_from_command(
            f"SET_HEATER_TEMPERATURE HEATER={name} TARGET={target}"
        )

    def _pause(self, seconds):
        self.reactor.pause(self.reactor.monotonic() + seconds)

    def _heaters_check(self):
        for name in HEATER_NAMES:
            target = self.temps[name]
            if target <= 0:
                continue
            if self._lookup_heater(name) is None:
                self.results['heaters'][name] = "Не найден"
                self.gcode.respond_info(f"Ошибка: нагреватель {name} не найден")
                continue
            self._heat_single_heater(name, target)

    def _heat_single_heater(self, name, target):
        try:
            self.gcode.respond_info(f"Нагрев {name} до {target}°C...")
            self._set_heater(name, target)
            self._wait_for_temperature(name, target)
            self.results['heaters'][name] = OK
            self.gcode.respond_info(f"{name} достиг {target}°C")
            if name in ('heater_bed', 'chamber'):
                self.gcode.respond_info(f"Отключение {name}...")
                self._set_heater(name, 0)
        except Exception as e:
            self.results['heaters'][name] = f"Ошибка: {e}"
            logging.error("Heater %s error: %s", name, e)
            self.gcode.respond_info(f"Ошибка нагрева {name}: {e}")

    def _wait_for_temperature(self, name, target, timeout=300.0, stall=30.0):
        last_temp = None
        start = self.reactor.monotonic()
        progress_at = start
        while True:
            current = self._get_temp(name)
            if current is None:
                self.gcode.respond_info(f"Не удалось получить температуру для {name}")
                current = 0.0
            if current >= target:
                return
            now = self.reactor.monotonic()
            if now > start + timeout:
                raise Exception("Таймаут нагрева")
            if last_temp is None or abs(current - last_temp) > 0.1:
                last_temp = current
                progress_at = now
            elif now > progress_at + stall:
                raise Exception("Нет прогресса нагрева")
            self._pause(0.1)

    def _park_axes(self):
        try:
            self.gcode.respond_info("Парковка всех осей (G28)...")
            self.gcode.run_script_from_command("G28")
            self.results['parking']['ALL'] = OK
        except Exception as e:
            self.results['parking']['ALL'] = f"Ошибка: {e}"
            self.gcode.respond_info(f"Ошибка парковки: {e}")
            self.results['skip_leveling'] = True

    def _bed_leveling(self):
        for cmd in ('G34', 'G29'):
            label = {
                'G34': "Выполнение G34 (выравнивание стола)...",
                'G29': "Выполнение G29 (создание карты стола)...",
            }[cmd]
            try:
                self.gcode.respond_info(label)
                self.gcode.run_script_from_command(cmd)
                self.results['leveling'][cmd] = OK
            except Exception as e:
                self.results['leveling'][cmd] = f"Ошибка: {e}"
                self.gcode.respond_info(f"Ошибка {cmd}: {e}")

    def _fan_check(self):
        if self._lookup_heater('extruder') is None:
            self.results['fan_check'] = "Пропущено (экструдер не найден)"
            self.gcode.respond_info("Пропуск проверки вентилятора: нагреватель экструдера не найден")
            return
        if self.results['heaters'].get('extruder') != OK:
            self.results['fan_check'] = "Пропущено (ошибка нагрева экструдера)"
            self.gcode.respond_info("Пропуск проверки вентилятора: экструдер не прошел проверку")
            return
        try:
            target = self.temps['extruder']
            self.gcode.respond_info(
                f"Нагрев экструдера до {target}°C для проверки вентилятора..."
            )
            self._set_heater('extruder', target)
            self._wait_for_temperature('extruder', target)

            self.gcode.respond_info(
                f"Ожидание стабилизации ({self.stabilization_time} сек)..."
            )
            self._pause(self.stabilization_time)

            self.gcode.respond_info("Перемещение в парковочную позицию...")
            p = self.park
            self.gcode.run_script_from_command(
                f"G1 X{p['x']} Y{p['y']} Z{p['z']} F6000"
            )
            self._pause(1.0)

            start_temp = self._get_temp('extruder')
            if start_temp is None:
                start_temp = target
            self.gcode.respond_info(f"Начальная температура: {start_temp:.1f}°C")

            self.gcode.respond_info("Включение вентилятора на 100%")
            self.gcode.run_script_from_command("M106 S255")
            self.gcode.respond_info(f"Ожидание {self.fan_check_time} сек...")

            deadline = self.reactor.monotonic() + self.fan_check_time
            min_temp = start_temp
            while self.reactor.monotonic() < deadline:
                self._pause(0.5)
                current = self._get_temp('extruder')
                if current is not None and current < min_temp:
                    min_temp = current

            self.gcode.run_script_from_command("M106 S0")
            drop = start_temp - min_temp
            self.gcode.respond_info(
                f"Мин. температура: {min_temp:.1f}°C, падение: {drop:.1f}°C"
            )
            if drop >= self.fan_check_drop:
                result = f"Успешно (падение: {drop:.1f}°C)"
            else:
                result = (
                    f"Сбой (падение: {drop:.1f}°C < {self.fan_check_drop}°C)"
                )
            self.results['fan_check'] = result
            self.gcode.respond_info(f"Проверка вентилятора: {result}")
        except Exception as e:
            try:
                self.gcode.run_script_from_command("M106 S0")
            except Exception:
                pass
            self.results['fan_check'] = f"Ошибка: {e}"
            logging.error("Fan check error: %s", e)
            self.gcode.respond_info(f"Ошибка проверки вентилятора: {e}")

    def _finalize(self):
        self.gcode.respond_info("Охлаждение нагревателей...")
        for name in HEATER_NAMES:
            try:
                self._set_heater(name, 0)
            except Exception:
                pass
        try:
            self.gcode.run_script_from_command("M106 S0")
        except Exception:
            pass

        report = ["=== ОТЧЕТ АВТОДИАГНОСТИКИ ===", "-- НАГРЕВАТЕЛИ --"]
        for name, status in self.results.get('heaters', {}).items():
            report.append(f"{name.upper()}: {status}")
        report.append("-- ПАРКОВКА ОСЕЙ --")
        for axis, status in self.results.get('parking', {}).items():
            report.append(f"{axis}: {status}")
        if self.results.get('skip_leveling'):
            report.append(
                "-- ВЫРАВНИВАНИЕ СТОЛА ПРОПУЩЕНО ИЗ-ЗА ОШИБОК ПЕРЕМЕЩЕНИЯ --"
            )
        else:
            report.append("-- ВЫРАВНИВАНИЕ СТОЛА --")
            for cmd, status in self.results.get('leveling', {}).items():
                report.append(f"{cmd}: {status}")
        report.append("-- ПРОВЕРКА ВЕНТИЛЯТОРА --")
        report.append(f"Результат: {self.results.get('fan_check', 'Не выполнена')}")
        report.append("Диагностика завершена!")
        report.append("=======================")
        self.gcode.respond_info("\n".join(report))


def load_config(config):
    return AutoDiagnostic(config)
