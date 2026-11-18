# Mock simulator service. It draws its camera pictures with NumPy on the CPU
# and contains no RoboDojo code, assets or simulator.
FROM python:3.12-slim
RUN pip install --no-cache-dir "numpy>=1.26,<3"
COPY simulator /opt/harbor-robodojo
ENV HARBOR_ROBODOJO_HOME=/opt/harbor-robodojo
ENV PYTHONPATH=/opt/harbor-robodojo
ENV PYTHONUNBUFFERED=1
CMD ["python", "-m", "robodojo.mock.service"]
