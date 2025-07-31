# Use the official AWS Lambda Python 3.11 base image
FROM public.ecr.aws/lambda/python:3.11

# Create a directory to hold the installed packages
WORKDIR /packages

# Copy the requirements file into the container
COPY requirements.txt .

# Install the packages from requirements.txt into the /packages directory
RUN pip install --target /packages -r requirements.txt

# Create a directory for the final zip contents
WORKDIR /asset
RUN cp -r /packages/. .