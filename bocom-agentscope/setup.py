from setuptools import find_packages, setup, Command
import os
import unittest
import subprocess


class RunShellScriptCommand(Command):
    user_options = [
        ('script=', 's', "Shell script file to run")
    ]

    def initialize_options(self):
        self.script = None

    def finalize_options(self):
        if self.script is None:
            raise ValueError("You must specify a shell script file to run with --script.")
        if not os.path.isfile(self.script):
            raise FileNotFoundError(f"The script file {self.script} does not exist.")

    def run(self):
        print(f"Running shell script file {self.script}")
        os.chmod(self.script, 0o755)
        result = subprocess.run([self.script], shell=True, check=True)
        if result.returncode != 0:
            raise RuntimeError(f"Shell script failed with return code {result.returncode}")
        else:
            print(f"Shell script completed successfully")


class UnitTestCommand(Command):
    user_options = [
        ('test-framework=', 'f', "Specify the test framework to use"),
        ('test-directory=', 'd', "Specify the directory to test"),
    ]

    def initialize_options(self):
        self.test_framework = 'unittest'
        self.test_directory = 'tests'

    def finalize_options(self):
        pass

    def run(self):
        if not os.path.isdir(self.test_directory):
            print(f"Test directory '{self.test_directory}' not found, skipping tests.")
            return
        if self.test_framework == 'unittest':
            loader = unittest.TestLoader()
            suite = loader.discover(start_dir=self.test_directory, pattern='*.py')
            runner = unittest.TextTestRunner(verbosity=2)
            result = runner.run(suite)
            print(result)
        elif self.test_framework == 'pytest':
            print("Pytest need write")
        else:
            raise ValueError('test-framework is nonsupport')


_HERE = os.path.dirname(os.path.abspath(__file__))
_SRC_DIR = os.path.join(_HERE, "src")


def parse_requirements(filename):
    filepath = os.path.join(_HERE, filename)
    requirements = []
    try:
        with open(filepath, 'r', encoding='utf-8') as f:
            for line in f:
                line = line.strip()
                if line and not line.startswith('#'):
                    requirements.append(line)
        return requirements
    except FileNotFoundError:
        print("Warning requirement file does not exist ")


def do_setup():
    os.chdir(_HERE)
    pkgs = find_packages(where="src")
    print(f"[setup.py] _HERE={_HERE}")
    print(f"[setup.py] packages={pkgs}")

    setup(
        name="bocom_agentscope",
        version="0.5.1",
        maintainer="EUVD Team",
        maintainer_email="w_jinchao@bankbocom.com",
        description="Bocom AgentScope SDK - config and providers for internal model platform",
        license="MIT",
        classifiers=["Programming Language :: Python"],
        package_dir={"": "src"},
        packages=pkgs,
        install_requires=parse_requirements("requirements.txt"),
        platforms="any",
        include_package_data=True,
        package_data={
            'providers': ['_models/*.yaml'],
        },
        cmdclass={
            'test': UnitTestCommand,
            'run_shell': RunShellScriptCommand,
        },
    )


if __name__ == '__main__':
    do_setup()
