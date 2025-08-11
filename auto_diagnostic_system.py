# Расширение для автоматической диагностики принтера в Klipper
#
# Copyright (C) 2025 Vlad Trigorlov <427departament@gmail.com>
#
# This file may be distributed under the terms of the GNU GPLv3 license.

import logging

class AutoDiagnostic:
    def __init__(self, config):
        self.printer = config.get_printer()
        self.gcode = self.printer.lookup_object('gcode')
        self.reactor = self.printer.get_reactor()
        self.toolhead = None
        self.results = {}
        # Загрузка параметров конфигурации
        self.temps = {
            'heater_bed': config.getfloat('bed_temp', 60.0, minval=0),
            'extruder': config.getfloat('extruder_temp', 200.0),
            'chamber': config.getfloat('chamber_temp', 0.0, minval=0)
        }
        self.park_positions = {
            'X': config.getfloat('park_x', 100.0),
            'Y': config.getfloat('park_y', 100.0),
            'Z': config.getfloat('park_z', 10.0)
        }
        self.fan_check_drop = config.getfloat('fan_temp_drop', 2.0, minval=0.5)
        self.fan_check_time = config.getfloat('fan_check_time', 15.0, minval=1.0)
        self.stabilization_time = config.getfloat('stabilization_time', 5.0, minval=0)
        # Регистрация команды G-кода
        self.gcode.register_command(
            'AUTO_DIAGNOSTIC',
            self.cmd_AUTO_DIAGNOSTIC,
            desc=self.cmd_AUTO_DIAGNOSTIC_help
        )
    cmd_AUTO_DIAGNOSTIC_help = "Запуск автоматической диагностики принтера"

    def cmd_AUTO_DIAGNOSTIC(self, gcmd):
        self.toolhead = self.printer.lookup_object('toolhead')
        self.results = {
            'heaters': {}, 'parking': {},
            'leveling': {}, 'fan_check': None,
            'skip_leveling': False
        }
        try:
            self.gcode.respond_info("=== ЗАПУСК АВТОДИАГНОСТИКИ ===")
            self._heaters_check()
            self._park_axes()
            self._fan_check()
            if not self.results['skip_leveling']:
                self._bed_leveling()
            #self._fan_check()
        except Exception as e:
            logging.exception("Критическая ошибка диагностики")
            self.gcode.respond_info(f"Критическая ошибка: {str(e)}")
        finally:
            self._finalize()
    def _heater_exists(self, heater_name):
        """Проверяет существование нагревателя в системе"""
        try:
            # Пытаемся получить объект нагревателя по имени
            obj = self.printer.lookup_object(heater_name)
            # Проверяем, есть ли у объекта метод get_status (характерно для нагревателей)
            if hasattr(obj, 'get_status'):
                return True
            # Если у объекта есть атрибут heater, тоже считаем его нагревателем
            if hasattr(obj, 'heater'):
                return True
            return False
        except:
            return False

    def _heaters_check(self):
        # Проверяем нагреватель стола отдельно
        if self.temps['heater_bed'] > 0:
            if self._heater_exists('heater_bed'):
                self._heat_single_heater('heater_bed', self.temps['heater_bed'])
            else:
                self.results['heaters']['heater_bed'] = "Не найден"
                self.gcode.respond_info("Ошибка: нагреватель стола (heater_bed) не найден")
        # Проверяем нагреватель камеры отдельно
        if self.temps['chamber'] > 0:
            if self._heater_exists('chamber'):
                self._heat_single_heater('chamber', self.temps['chamber'])
            else:
                self.results['heaters']['chamber'] = "Не найден"
                self.gcode.respond_info("Ошибка: нагреватель камеры (chamber) не найден")
        # Проверяем основной экструдер отдельно
        if self.temps['extruder'] > 0:
            if self._heater_exists('extruder'):
                self._heat_single_heater('extruder', self.temps['extruder'])
            else:
                self.results['heaters']['extruder'] = "Не найден"
                self.gcode.respond_info("Ошибка: нагреватель экструдера (extruder) не найден")

    def _heat_single_heater(self, heater_name, target_temp):
        try:
            self.gcode.respond_info(f"Нагрев {heater_name} до {target_temp}°C...")
            self.gcode.run_script_from_command(f"SET_HEATER_TEMPERATURE HEATER={heater_name} TARGET={target_temp}")
            self._wait_for_temperature(heater_name, target_temp)
            self.results['heaters'][heater_name] = "Успешно"
            self.gcode.respond_info(f"{heater_name} достиг {target_temp}°C")
            # Отключаем стол и камеру после нагрева
            if heater_name in ['heater_bed', 'chamber']:
                self.gcode.respond_info(f"Отключение {heater_name}...")
                self.gcode.run_script_from_command(f"SET_HEATER_TEMPERATURE HEATER={heater_name} TARGET=0")
        except Exception as e:
            msg = f"Ошибка: {str(e)}"
            self.results['heaters'][heater_name] = msg
            logging.error(f"Heater {heater_name} error: {e}")
            self.gcode.respond_info(f"Ошибка нагрева {heater_name}: {str(e)}")
    def _wait_for_temperature(self, heater_name, target):
        tolerance = 0
        last_temp = 0
        start_time = self.reactor.monotonic()
        start_time_progress = start_time
        try:
            heater = self.printer.lookup_object(heater_name)
        except:
            heater = None
        while True:
            current_temp = 0
            if heater:
                try:
                    # Попробуем получить температуру разными способами
                    if hasattr(heater, 'get_temp'):
                        current_temp, _ = heater.get_temp()
                    elif hasattr(heater, 'get_status'):
                        status = heater.get_status(self.reactor.monotonic())
                        current_temp = status['temperature']
                    elif hasattr(heater, 'heater') and hasattr(heater.heater, 'get_status'):
                        status = heater.heater.get_status(self.reactor.monotonic())
                        current_temp = status['temperature']
                    else:
                        self.gcode.respond_info(f"Не удалось получить температуру для {heater_name}")
                        current_temp = 0
                except Exception as e:
                    self.gcode.respond_info(f"Ошибка получения температуры: {str(e)}")
                    current_temp = 0
            self.reactor.pause(self.reactor.monotonic() + 0.1)
            if current_temp >= target - tolerance:
                return
            if self.reactor.monotonic() > start_time + 300:
                raise Exception("Таймаут нагрева")
            if abs(current_temp - last_temp) > 0.1:
                last_temp = current_temp
                start_time_progress = self.reactor.monotonic()
            elif self.reactor.monotonic() > start_time_progress + 30:
                raise Exception("Нет прогресса нагрева")

    def _park_axes(self):
        self.results['parking'] = {}
        try:
            self.gcode.respond_info("Парковка всех осей (G28)...")
            self.gcode.run_script_from_command("G28")
            self.results['parking']['ALL'] = "Успешно"
        except Exception as e:
            msg = f"Ошибка: {str(e)}"
            self.results['parking']['ALL'] = msg
            self.gcode.respond_info(f"Ошибка парковки: {str(e)}")
            self.results['skip_leveling'] = True
    def _bed_leveling(self):
        self.results['leveling'] = {}
        try:
            self.gcode.respond_info("Выполнение G34 (выравнивание стола)...")
            self.gcode.run_script_from_command("G34")
            self.results['leveling']['G34'] = "Успешно"
        except Exception as e:
            self.results['leveling']['G34'] = f"Ошибка: {str(e)}"
            self.gcode.respond_info(f"Ошибка G34: {str(e)}")
        try:
            self.gcode.respond_info("Выполнение G29 (создание карты стола)...")
            self.gcode.run_script_from_command("G29")
            self.results['leveling']['G29'] = "Успешно"
        except Exception as e:
            self.results['leveling']['G29'] = f"Ошибка: {str(e)}"
            self.gcode.respond_info(f"Ошибка G29: {str(e)}")
    def _fan_check(self):
        # Проверяем наличие экструдера
        if not self._heater_exists('extruder'):
            self.gcode.respond_info("Пропуск проверки вентилятора: нагреватель экструдера не найден")
            self.results['fan_check'] = "Пропущено (экструдер не найден)"
            return
        # Проверяем успешность нагрева экструдера
        if 'extruder' not in self.results['heaters'] or self.results['heaters']['extruder'] != "Успешно":
            self.gcode.respond_info("Пропуск проверки вентилятора: экструдер не прошел проверку")
            self.results['fan_check'] = "Пропущено (ошибка нагрева экструдера)"
            return
        try:
            # Нагрев экструдера
            target_temp = self.temps['extruder']
            self.gcode.respond_info(f"Нагрев экструдера до {target_temp}°C для проверки вентилятора...")
            self.gcode.run_script_from_command(f"SET_HEATER_TEMPERATURE HEATER=extruder TARGET={target_temp}")
            self._wait_for_temperature('extruder', target_temp)
            # Стабилизация температуры
            self.gcode.respond_info(f"Ожидание стабилизации ({self.stabilization_time} сек)...")
            start_stable = self.reactor.monotonic()
            while self.reactor.monotonic() < start_stable + self.stabilization_time:
                self.reactor.pause(self.reactor.monotonic() + 0.5)
            # Перемещение в парковочную позицию
            self.gcode.respond_info("Перемещение в парковочную позицию...")
            park_x = self.park_positions['X']
            park_y = self.park_positions['Y']
            park_z = self.park_positions['Z']
            self.gcode.run_script_from_command(f"G1 X{park_x} Y{park_y} Z{park_z} F6000")
            self.reactor.pause(self.reactor.monotonic() + 1.0)
            # Получение начальной температуры
            try:
                extruder = self.printer.lookup_object('extruder')
                if hasattr(extruder, 'get_temp'):
                    start_temp, _ = extruder.get_temp()
                elif hasattr(extruder, 'heater') and hasattr(extruder.heater, 'get_status'):
                    status = extruder.heater.get_status(self.reactor.monotonic())
                    start_temp = status['temperature']
                else:
                    start_temp = target_temp
            except:
                start_temp = target_temp
            self.gcode.respond_info(f"Начальная температура: {start_temp:.1f}°C")
            # Включение вентилятора
            self.gcode.respond_info("Включение вентилятора на 100%")
            self.gcode.run_script_from_command("M106 S255")
            self.gcode.respond_info(f"Ожидание {self.fan_check_time} сек...")
            start_time = self.reactor.monotonic()
            min_temp = start_temp
            while self.reactor.monotonic() < start_time + self.fan_check_time:
                self.reactor.pause(self.reactor.monotonic() + 0.5)
                # Получение текущей температуры
                try:
                    if hasattr(extruder, 'get_temp'):
                        current_temp, _ = extruder.get_temp()
                    elif hasattr(extruder, 'heater') and hasattr(extruder.heater, 'get_status'):
                        status = extruder.heater.get_status(self.reactor.monotonic())
                        current_temp = status['temperature']
                    else:
                        current_temp = min_temp
                except:
                    current_temp = min_temp
                if current_temp < min_temp:
                    min_temp = current_temp
            # Выключение вентилятора
            self.gcode.run_script_from_command("M106 S0")
            temp_drop = start_temp - min_temp
            self.gcode.respond_info(f"Мин. температура: {min_temp:.1f}°C, падение: {temp_drop:.1f}°C")
            if temp_drop >= self.fan_check_drop:
                result = f"Успешно (падение: {temp_drop:.1f}°C)"
            else:
                result = f"Сбой (падение: {temp_drop:.1f}°C < {self.fan_check_drop}°C)"
            self.results['fan_check'] = result
            self.gcode.respond_info(f"Проверка вентилятора: {result}")
        except Exception as e:
            try:
                self.gcode.run_script_from_command("M106 S0")
            except:
                pass
            self.results['fan_check'] = f"Ошибка: {str(e)}"
            logging.error(f"Fan check error: {e}")
            self.gcode.respond_info(f"Ошибка проверки вентилятора: {str(e)}")
    def _finalize(self):
        self.gcode.respond_info("Охлаждение нагревателей...")
        # Выключение всех нагревателей
        heaters = ['extruder', 'heater_bed', 'chamber']
        for heater_name in heaters:
            try:
                self.gcode.run_script_from_command(f"SET_HEATER_TEMPERATURE HEATER={heater_name} TARGET=0")
            except:
                pass
        # Выключение вентилятора
        try:
            self.gcode.run_script_from_command("M106 S0")
        except:
            pass
        report = ["=== ОТЧЕТ АВТОДИАГНОСТИКИ ==="]
        report.append("-- НАГРЕВАТЕЛИ --")
        for name, status in self.results.get('heaters', {}).items():
            report.append(f"{name.upper()}: {status}")
        report.append("-- ПАРКОВКА ОСЕЙ --")
        for axis, status in self.results.get('parking', {}).items():
            report.append(f"{axis}: {status}")
        if not self.results.get('skip_leveling', True):
            report.append("-- ВЫРАВНИВАНИЕ СТОЛА --")
            for cmd, status in self.results.get('leveling', {}).items():
                report.append(f"{cmd}: {status}")
        else:
            report.append("-- ВЫРАВНИВАНИЕ СТОЛА ПРОПУЩЕНО ИЗ-ЗА ОШИБОК ПЕРЕМЕЩЕНИЯ --")
        report.append("-- ПРОВЕРКА ВЕНТИЛЯТОРА --")
        report.append(f"Результат: {self.results.get('fan_check', 'Не выполнена')}")
        report.append("Диагностика завершена!")
        report.append("=======================")
        self.gcode.respond_info("\n".join(report))

def load_config(config):
    return AutoDiagnostic(config)