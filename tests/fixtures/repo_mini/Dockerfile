FROM openjdk:11-jre-slim

EXPOSE 8080
ENV DB_URL=postgres://db:5432/mydb

COPY app.jar /app.jar
CMD ["java", "-jar", "/app.jar"]
